"""Tree-sitter function fingerprints for JavaScript and TypeScript clone comparison."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from deadcode_audit.clones import FuncRecord, semantic_signature

PARSER_VERSION = "0.25.2"

_FUNCTION_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function",
        "arrow_function",
        "method_definition",
    }
)
_DECLARATION_FUNCTION_TYPES = frozenset(
    {"function_declaration", "generator_function_declaration"}
)
_TRANSPARENT_EXPRESSION_TYPES = frozenset(
    {
        "parenthesized_expression",
        "as_expression",
        "satisfies_expression",
        "non_null_expression",
        "type_assertion",
        "instantiation_expression",
    }
)
_TYPE_ONLY_TYPES = frozenset(
    {
        "type_annotation",
        "return_type",
        "type_parameters",
        "type_arguments",
        "type_predicate",
        "implements_clause",
        "type_alias_declaration",
        "interface_declaration",
        "ambient_declaration",
    }
)
_PARAMETER_WRAPPER_TYPES = frozenset(
    {"required_parameter", "optional_parameter", "parameter"}
)
_PATTERN_TYPES = frozenset(
    {
        "identifier",
        "shorthand_property_identifier_pattern",
        "object_pattern",
        "array_pattern",
        "pair_pattern",
        "assignment_pattern",
        "object_assignment_pattern",
        "rest_pattern",
        "required_parameter",
        "optional_parameter",
        "parameter",
    }
)
_LEXICAL_SCOPE_TYPES = frozenset(
    {
        "statement_block",
        "catch_clause",
        "for_statement",
        "for_in_statement",
        "switch_body",
        "class_body",
    }
)
_LITERAL_TYPES = {
    "number": "<number>",
    "string": "<string>",
    "string_fragment": "<string>",
    "regex": "<regex>",
    "true": "<boolean>",
    "false": "<boolean>",
    "null": "<null>",
    "jsx_text": "<jsx-text>",
}


def _load_parser(path: Path):
    """Load the selected local grammar only when the optional frontend is used."""
    try:
        import tree_sitter_javascript
        import tree_sitter_typescript
        from tree_sitter import Language, Parser
    except ImportError as exc:
        raise ImportError(
            "JavaScript/TypeScript function analysis requires the optional local "
            "Tree-sitter grammars; install with `uv sync --all-extras`"
        ) from exc

    if metadata.version("tree-sitter") != PARSER_VERSION:
        raise RuntimeError(
            f"JS/TS extraction requires tree-sitter{PARSER_VERSION}; "
            "0.26.0 has an upstream Point getter reference-count corruption defect"
        )
    typescript = path.suffix.lower() in {".ts", ".tsx", ".cts", ".mts"}
    if typescript:
        capsule = (
            tree_sitter_typescript.language_tsx()
            if path.suffix.lower() == ".tsx"
            else tree_sitter_typescript.language_typescript()
        )
    else:
        capsule = tree_sitter_javascript.language()
    return Parser(Language(capsule)), typescript


def _node_key(node) -> tuple[int, int, str]:
    return node.start_byte, node.end_byte, node.type


def _source_text(buffer: bytes, node) -> str:
    return buffer[node.start_byte : node.end_byte].decode("utf-8")


def _location(buffer: bytes, offset: int) -> tuple[int, int]:
    line_start = buffer.rfind(b"\n", 0, offset) + 1
    line = buffer.count(b"\n", 0, offset) + 1
    column = len(buffer[line_start:offset].decode("utf-8")) + 1
    return line, column


def _parse_issues(root):
    issues = []
    pending = [root]
    while pending:
        node = pending.pop()
        if node.is_error or node.is_missing:
            issues.append(node)
        pending.extend(node.children)
    return issues


_JSX_PARENTS = frozenset({"jsx_element", "jsx_fragment"})


def _is_stray_jsx_ampersand(token, data: bytearray) -> bool:
    """Whether ``token`` is an ``&``-only leaf that error recovery split out of JSX text."""
    if token.child_count or token.is_named or set(data[token.start_byte : token.end_byte]) != {0x26}:
        return False
    before = token.prev_sibling
    if before is None:
        return token.parent.parent is not None and token.parent.parent.type in _JSX_PARENTS
    return before.type.startswith("jsx_") and before.type != "jsx_closing_element"


def _repair_jsx_ampersands(parser, buffer: bytes):
    """Parse ``buffer``; work around the grammars' refusal of a bare ``&`` in JSX text.

    Both tree-sitter-javascript and the tsx grammar lex ``&`` only as the start of a complete HTML
    entity, so valid JSX such as ``<p>a & b</p>`` yields an ERROR node. Only ``&``-only tokens inside
    an ERROR node that directly follow JSX content (or sit first under a JSX element/fragment) are
    touched: each is replaced by ``_`` (same byte length, so every offset, column and span is
    unchanged) and the buffer re-parsed. Returns ``(tree, analysed_buffer)``; the caller still owns
    the original bytes.
    """
    data = bytearray(buffer)
    while True:  # each round replaces >= 1 '&', so it ends
        tree = parser.parse(bytes(data))
        fixes = [
            (token.start_byte, token.end_byte)
            for issue in _parse_issues(tree.root_node)
            if issue.is_error
            for token in issue.children
            if _is_stray_jsx_ampersand(token, data)
        ]
        if not fixes:
            return tree, bytes(data)
        for start, end in fixes:
            data[start:end] = b"_" * (end - start)


def _parse_failure(
    root, buffer: bytes, path: Path, typescript: bool, issues
) -> SyntaxError:
    first = min(issues, key=lambda node: node.start_byte) if issues else root
    line, column = _location(buffer, first.start_byte)
    language = "TypeScript" if typescript else "JavaScript"
    return SyntaxError(f"{path}:{line}:{column}: invalid {language} syntax")


@dataclass
class _Scope:
    kind: str
    parent: _Scope | None
    candidates: list[tuple[int, str, tuple[int, int, str]]] = field(
        default_factory=list
    )
    bindings: dict[str, str] = field(default_factory=dict)

    @property
    def var_scope(self) -> _Scope:
        scope = self
        while scope.kind not in {"function", "static"} and scope.parent is not None:
            scope = scope.parent
        return scope


def _pattern_identifiers(node):
    """Yield binding identifiers, not property keys or computed-key expressions."""
    if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
        yield node
        return
    if node.type == "pair_pattern":
        value = node.child_by_field_name("value")
        if value is not None:
            yield from _pattern_identifiers(value)
        return
    if node.type == "assignment_pattern":
        left = node.child_by_field_name("left")
        if left is not None:
            yield from _pattern_identifiers(left)
        return
    if node.type == "object_assignment_pattern":
        left = node.child_by_field_name("left")
        if left is not None:
            yield from _pattern_identifiers(left)
        else:
            for child in node.named_children:
                if child.type in _PATTERN_TYPES:
                    yield from _pattern_identifiers(child)
        return
    if node.type in _PATTERN_TYPES:
        for child in node.named_children:
            if child.type in _PATTERN_TYPES:
                yield from _pattern_identifiers(child)


def _parameter_patterns(function_node):
    parameters = function_node.child_by_field_name("parameters")
    if parameters is not None:
        for child in parameters.named_children:
            if child.type in _PARAMETER_WRAPPER_TYPES:
                pattern = child.child_by_field_name("pattern")
                if pattern is None:
                    pattern = next(
                        (
                            item
                            for item in child.named_children
                            if item.type in _PATTERN_TYPES
                        ),
                        None,
                    )
                if pattern is not None:
                    yield pattern
            elif child.type in _PATTERN_TYPES:
                yield child
    parameter = function_node.child_by_field_name("parameter")
    if parameter is not None and parameter.type in _PATTERN_TYPES:
        yield parameter


def _pattern_names(pattern, buffer: bytes) -> list[str]:
    return [_source_text(buffer, node) for node in _pattern_identifiers(pattern)]


def _binding_label(pattern, buffer: bytes) -> str | None:
    names = _pattern_names(pattern, buffer)
    return ",".join(names) if names else None


def _build_scopes(function_node, buffer: bytes):
    root_scope = _Scope("function", None)
    scopes = [root_scope]
    scope_for_node: dict[tuple[int, int, str], _Scope] = {}
    function_scopes: dict[tuple[int, int, str], _Scope] = {}

    def visit(node, scope: _Scope, is_root: bool = False):
        if not is_root and node.type in _FUNCTION_TYPES:
            scope = _Scope("function", scope)
            scopes.append(scope)
            function_scopes[_node_key(node)] = scope
        elif not is_root and node.type == "class_static_block":
            scope = _Scope("static", scope)
            scopes.append(scope)
        elif not is_root and node.type in _LEXICAL_SCOPE_TYPES:
            scope = _Scope("block", scope)
            scopes.append(scope)
        scope_for_node[_node_key(node)] = scope
        for child in node.children:
            visit(child, scope)

    function_scopes[_node_key(function_node)] = root_scope
    visit(function_node, root_scope, is_root=True)

    def add_binding(identifier, scope: _Scope) -> None:
        name = _source_text(buffer, identifier)
        scope.candidates.append((identifier.start_byte, name, _node_key(identifier)))
        scope_for_node[_node_key(identifier)] = scope

    pending = [function_node]
    while pending:
        node = pending.pop()
        node_scope = scope_for_node[_node_key(node)]

        if node.type in _FUNCTION_TYPES:
            function_scope = function_scopes[_node_key(node)]
            name_node = node.child_by_field_name("name")
            if name_node is not None and node.type in (
                _DECLARATION_FUNCTION_TYPES
                | {"function_expression", "generator_function"}
            ):
                if (
                    node is function_node
                    or node.type not in _DECLARATION_FUNCTION_TYPES
                ):
                    add_binding(name_node, function_scope)
                else:
                    add_binding(name_node, function_scope.parent or function_scope)
            for pattern in _parameter_patterns(node):
                for identifier in _pattern_identifiers(pattern):
                    add_binding(identifier, function_scope)

        elif node.type in {"class_declaration", "class"}:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                binding_scope = node_scope
                body = node.child_by_field_name("body")
                if node.type == "class" and body is not None:
                    binding_scope = scope_for_node[_node_key(body)]
                add_binding(name_node, binding_scope)

        elif node.type == "catch_clause":
            parameter = node.child_by_field_name("parameter")
            if parameter is not None:
                for identifier in _pattern_identifiers(parameter):
                    add_binding(identifier, node_scope)

        elif node.type == "variable_declarator":
            pattern = node.child_by_field_name("name")
            if pattern is not None:
                declaration = node.parent
                if (
                    declaration is not None
                    and declaration.type == "variable_declaration"
                ):
                    target_scope = node_scope.var_scope
                else:
                    target_scope = node_scope
                for identifier in _pattern_identifiers(pattern):
                    add_binding(identifier, target_scope)

        pending.extend(reversed(node.children))

    next_placeholder = 0
    for scope in scopes:
        for _, name, _ in sorted(scope.candidates):
            if name not in scope.bindings:
                scope.bindings[name] = f"V{next_placeholder}"
                next_placeholder += 1

    return scope_for_node


def _resolve_local(
    node, scope_for_node: dict[tuple[int, int, str], _Scope]
) -> str | None:
    scope = scope_for_node.get(_node_key(node))
    name = node.text.decode("utf-8")
    while scope is not None:
        if name in scope.bindings:
            return scope.bindings[name]
        scope = scope.parent
    return None


def _literal_token(node, buffer: bytes) -> str:
    literal = node
    if (
        node.type == "string_fragment"
        and node.parent is not None
        and node.parent.type == "string"
    ):
        literal = node.parent
    parent = literal.parent
    is_member_key = parent is not None and (
        (
            parent.type == "subscript_expression"
            and parent.child_by_field_name("index") == literal
        )
        or (parent.type == "pair" and parent.child_by_field_name("key") == literal)
        or (
            parent.type == "method_definition"
            and parent.child_by_field_name("name") == literal
        )
    )
    if is_member_key:
        return f".{_source_text(buffer, literal)}"
    return _LITERAL_TYPES[node.type]


def _runtime_body_tokens(function_node, buffer: bytes) -> tuple[str, ...]:
    body = function_node.child_by_field_name("body")
    if body is None:
        return ()
    scope_for_node = _build_scopes(function_node, buffer)
    tokens: list[str] = []

    def emit(node) -> None:
        if node.type == "comment" or node.type.endswith("_comment"):
            return
        if node.type in _TYPE_ONLY_TYPES:
            return
        if node.type in _TRANSPARENT_EXPRESSION_TYPES:
            named = [
                child
                for child in node.named_children
                if child.type not in _TYPE_ONLY_TYPES
            ]
            if named:
                emit(named[0] if node.type != "type_assertion" else named[-1])
            return
        if node.type in _PARAMETER_WRAPPER_TYPES:
            for child in node.children:
                if child.type not in _TYPE_ONLY_TYPES:
                    emit(child)
            return

        tokens.append(f"({node.type}")
        if node.child_count == 0:
            if node.type == "identifier":
                local = _resolve_local(node, scope_for_node)
                tokens.append(
                    local if local is not None else f"#{_source_text(buffer, node)}"
                )
            elif node.type in {"property_identifier", "private_property_identifier"}:
                tokens.append(f".{_source_text(buffer, node)}")
            elif node.type in {
                "shorthand_property_identifier_pattern",
                "shorthand_property_identifier",
            }:
                local = _resolve_local(node, scope_for_node)
                value = local or f"#{_source_text(buffer, node)}"
                tokens.extend((f".{_source_text(buffer, node)}", value))
            elif node.type in _LITERAL_TYPES:
                tokens.append(_literal_token(node, buffer))
            elif node.type == "jsx_identifier":
                tokens.append(f"#jsx:{_source_text(buffer, node)}")
            elif node.type == "undefined":
                tokens.append("#undefined")
            else:
                tokens.append(node.type)
        else:
            for child in node.children:
                emit(child)
        tokens.append(")")

    mode_tokens = []
    if any(child.type == "async" for child in function_node.children):
        mode_tokens.append("MODE:async")
    if function_node.type in {
        "generator_function",
        "generator_function_declaration",
    } or any(child.type == "*" for child in function_node.children):
        mode_tokens.append("MODE:generator")
    tokens.extend(mode_tokens)
    emit(body)
    return tuple(tokens)


def _transparent_parent(node):
    current = node
    while (
        current.parent is not None
        and current.parent.type in _TRANSPARENT_EXPRESSION_TYPES
    ):
        current = current.parent
    return current


def _owner_symbol(container, buffer: bytes) -> str | None:
    expression = _transparent_parent(container)
    parent = expression.parent
    if parent is None:
        return None
    if (
        parent.type == "variable_declarator"
        and parent.child_by_field_name("value") == expression
    ):
        pattern = parent.child_by_field_name("name")
        return _binding_label(pattern, buffer) if pattern is not None else None
    if (
        parent.type == "assignment_expression"
        and parent.child_by_field_name("right") == expression
    ):
        target = parent.child_by_field_name("left")
        return _source_text(buffer, target) if target is not None else None
    if parent.type == "pair" and parent.child_by_field_name("value") == expression:
        key = parent.child_by_field_name("key")
        outer = parent.parent
        if key is not None and outer is not None and outer.type == "object":
            prefix = _owner_symbol(outer, buffer)
            key_text = _source_text(buffer, key)
            return f"{prefix}.{key_text}" if prefix else key_text
    return None


def _class_qualifier(class_node, buffer: bytes) -> str | None:
    owned = _owner_symbol(class_node, buffer)
    if owned:
        return owned
    name = class_node.child_by_field_name("name")
    if name is not None:
        return _source_text(buffer, name)
    return None


def _method_symbol(node, buffer: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is None:
        return None
    method = _source_text(buffer, name)
    parent = node.parent
    if parent is None:
        return method
    if parent.type == "class_body" and parent.parent is not None:
        prefix = _class_qualifier(parent.parent, buffer)
        if prefix:
            return f"{prefix}.{method}"
        line, column = _location(buffer, parent.parent.start_byte)
        return f"class@{line}:{column}.{method}"
    if parent.type == "object":
        prefix = _owner_symbol(parent, buffer)
        if prefix:
            return f"{prefix}.{method}"
        line, column = _location(buffer, parent.start_byte)
        return f"object@{line}:{column}.{method}"
    return method


def _symbol(function_node, buffer: bytes) -> str:
    if function_node.type == "method_definition":
        symbol = _method_symbol(function_node, buffer)
        if symbol:
            return symbol

    owned = _owner_symbol(function_node, buffer)
    if owned:
        return owned

    name = function_node.child_by_field_name("name")
    if name is not None:
        return _source_text(buffer, name)

    line, column = _location(buffer, function_node.start_byte)
    return f"anonymous@{line}:{column}"


def collect_functions(source: str, path: Path) -> list[tuple[FuncRecord, str]]:
    """Extract executable JS/TS functions and exact UTF-8 source spans for clone analysis.

    The whole captured buffer is parsed and validated before any records are returned. This
    function does not execute source or load model weights; it only builds syntax fingerprints.
    """
    buffer = source.encode("utf-8")
    parser, typescript = _load_parser(path)
    tree, analysed = _repair_jsx_ampersands(parser, buffer)
    root = tree.root_node
    issues = _parse_issues(root)
    if root.has_error or issues:
        raise _parse_failure(root, buffer, path, typescript, issues)

    functions = []
    pending = [root]
    while pending:
        node = pending.pop()
        if (
            node.type in _FUNCTION_TYPES
            and node.child_by_field_name("body") is not None
        ):
            functions.append(node)
        pending.extend(reversed(node.children))

    functions.sort(key=lambda node: (node.start_byte, -node.end_byte))
    records = []
    for node in functions:
        tokens = _runtime_body_tokens(node, analysed)
        token_tuple = tuple(tokens)
        record = FuncRecord(
            file_path=path,
            line=node.start_point.row + 1,
            symbol=_symbol(node, buffer),
            tokens=token_tuple,
            semantic=semantic_signature(token_tuple),
            language="javascript",
            column=_location(buffer, node.start_byte)[1],
            start_byte=node.start_byte,
            end_byte=node.end_byte,
        )
        snippet = buffer[node.start_byte : node.end_byte].decode("utf-8")
        records.append((record, snippet))
    return records
