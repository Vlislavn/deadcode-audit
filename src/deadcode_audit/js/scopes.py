"""Lexical scopes, binding resolution and the identifier-blind runtime token stream of a JS/TS function."""

from __future__ import annotations

from dataclasses import dataclass, field

from deadcode_audit.js.parser import node_key, source_text

FUNCTION_TYPES = frozenset(
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


TRANSPARENT_EXPRESSION_TYPES = frozenset(
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
    return [source_text(buffer, node) for node in _pattern_identifiers(pattern)]


def binding_label(pattern, buffer: bytes) -> str | None:
    names = _pattern_names(pattern, buffer)
    return ",".join(names) if names else None


def _build_scopes(function_node, buffer: bytes):
    root_scope = _Scope("function", None)
    scopes = [root_scope]
    scope_for_node: dict[tuple[int, int, str], _Scope] = {}
    function_scopes: dict[tuple[int, int, str], _Scope] = {}

    def visit(node, scope: _Scope, is_root: bool = False):
        if not is_root and node.type in FUNCTION_TYPES:
            scope = _Scope("function", scope)
            scopes.append(scope)
            function_scopes[node_key(node)] = scope
        elif not is_root and node.type == "class_static_block":
            scope = _Scope("static", scope)
            scopes.append(scope)
        elif not is_root and node.type in _LEXICAL_SCOPE_TYPES:
            scope = _Scope("block", scope)
            scopes.append(scope)
        scope_for_node[node_key(node)] = scope
        for child in node.children:
            visit(child, scope)

    function_scopes[node_key(function_node)] = root_scope
    visit(function_node, root_scope, is_root=True)

    def add_binding(identifier, scope: _Scope) -> None:
        name = source_text(buffer, identifier)
        scope.candidates.append((identifier.start_byte, name, node_key(identifier)))
        scope_for_node[node_key(identifier)] = scope

    pending = [function_node]
    while pending:
        node = pending.pop()
        node_scope = scope_for_node[node_key(node)]

        if node.type in FUNCTION_TYPES:
            function_scope = function_scopes[node_key(node)]
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
                    binding_scope = scope_for_node[node_key(body)]
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
    scope = scope_for_node.get(node_key(node))
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
        return f".{source_text(buffer, literal)}"
    return _LITERAL_TYPES[node.type]


def runtime_body_tokens(function_node, buffer: bytes, sizes: dict | None = None) -> tuple[str, ...]:
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
        if node.type in TRANSPARENT_EXPRESSION_TYPES:
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

        start = len(tokens)
        tokens.append(f"({node.type}")
        if node.child_count == 0:
            if node.type == "identifier":
                local = _resolve_local(node, scope_for_node)
                tokens.append(
                    local if local is not None else f"#{source_text(buffer, node)}"
                )
            elif node.type in {"property_identifier", "private_property_identifier"}:
                tokens.append(f".{source_text(buffer, node)}")
            elif node.type in {
                "shorthand_property_identifier_pattern",
                "shorthand_property_identifier",
            }:
                local = _resolve_local(node, scope_for_node)
                value = local or f"#{source_text(buffer, node)}"
                tokens.extend((f".{source_text(buffer, node)}", value))
            elif node.type in _LITERAL_TYPES:
                tokens.append(_literal_token(node, buffer))
            elif node.type == "jsx_identifier":
                tokens.append(f"#jsx:{source_text(buffer, node)}")
            elif node.type == "undefined":
                tokens.append("#undefined")
            else:
                tokens.append(node.type)
        else:
            for child in node.children:
                emit(child)
        tokens.append(")")
        if sizes is not None:
            sizes[node_key(node)] = len(tokens) - start

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
