"""Tree-sitter function fingerprints for JavaScript and TypeScript clone comparison."""

from __future__ import annotations

from pathlib import Path

from deadcode_audit.overlap.blind import rename
from deadcode_audit.overlap.clones import FuncRecord, semantic_signature
from deadcode_audit.overlap.js_parser import (
    load_parser,
    location,
    node_key,
    parse_failure,
    parse_issues,
    repair_jsx_ampersands,
    source_text,
)
from deadcode_audit.overlap.js_scopes import (
    FUNCTION_TYPES,
    TRANSPARENT_EXPRESSION_TYPES,
    binding_label,
    runtime_body_tokens,
)

# Identifier leaves renamed in the identifier-blind text (blind.py RULE): names, properties, shorthand properties.
_IDENTIFIER_LEAF_TYPES = frozenset(
    {
        "identifier",
        "property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
        "private_property_identifier",
    }
)


_INVOCATION_TYPES = frozenset({"call_expression", "new_expression", "jsx_opening_element", "jsx_self_closing_element"})


def _callee_name(node, buffer: bytes, aliases: dict[str, str]) -> str | None:
    """Name invoked by a call/new/JSX-component node: an identifier or the last property of a member chain."""
    if node.type == "call_expression":
        target = node.child_by_field_name("function")
    elif node.type == "new_expression":
        target = node.child_by_field_name("constructor")
    else:
        target = node.child_by_field_name("name")
    while target is not None and target.type in TRANSPARENT_EXPRESSION_TYPES and target.named_children:
        target = target.named_children[0]
    if target is None:
        return None
    if target.type == "member_expression" and node.type != "jsx_opening_element":
        target = target.child_by_field_name("property")
    if target is None or target.type not in {"identifier", "property_identifier"}:
        return None
    name = source_text(buffer, target)
    if node.type.startswith("jsx_") and not name[:1].isupper():
        return None  # lowercase JSX tags are DOM elements, not component invocations
    return aliases.get(name, name)


def _removed_with_invocation(node):
    """Nodes deleted with one invocation: its statement when the call IS the statement's value, else just the call."""
    if node.type == "jsx_opening_element":
        closing = node.parent.child_by_field_name("close_tag") if node.parent is not None else None
        return [node, closing] if closing is not None else [node]
    current = node
    while current.parent is not None and current.parent.type in {"await_expression", "parenthesized_expression"}:
        current = current.parent
    parent = current.parent
    if parent is None:
        return [node]
    if parent.type in {"expression_statement", "return_statement"}:
        return [parent]
    declaration = parent.parent
    if parent.type == "variable_declarator" and declaration is not None and len(declaration.named_children) == 1:
        return [declaration]
    if parent.type == "assignment_expression" and parent.child_by_field_name("right") == current:
        statement = parent.parent
        return [statement] if statement is not None and statement.type == "expression_statement" else [node]
    return [node]


def _invocation_remainders(function_node, buffer: bytes, sizes: dict, total: int, aliases: dict[str, str]):
    """``(callee, own tokens left)`` for every name the function invokes (nested closures included)."""
    removed: dict[str, dict[tuple[int, int, str], int]] = {}
    pending = [function_node.child_by_field_name("body")]
    while pending:
        node = pending.pop()
        if node is None:
            continue
        if node.type in _INVOCATION_TYPES:
            name = _callee_name(node, buffer, aliases)
            if name is not None:
                bucket = removed.setdefault(name, {})
                for target in _removed_with_invocation(node):
                    bucket[node_key(target)] = sizes.get(node_key(target), 0)
        pending.extend(node.children)
    result = []
    for name, bucket in removed.items():
        spans = sorted(bucket, key=lambda key: (key[0], -key[1]))
        cut, covered_to = 0, -1
        for start, end, kind in spans:  # nested removals (B(B(x))) are counted once, by their outermost node
            if start >= covered_to:
                cut += bucket[(start, end, kind)]
                covered_to = end
        result.append((name, total - cut))
    return tuple(sorted(result))


def _transparent_parent(node):
    current = node
    while (
        current.parent is not None
        and current.parent.type in TRANSPARENT_EXPRESSION_TYPES
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
        return binding_label(pattern, buffer) if pattern is not None else None
    if (
        parent.type == "assignment_expression"
        and parent.child_by_field_name("right") == expression
    ):
        target = parent.child_by_field_name("left")
        return source_text(buffer, target) if target is not None else None
    if parent.type == "pair" and parent.child_by_field_name("value") == expression:
        key = parent.child_by_field_name("key")
        outer = parent.parent
        if key is not None and outer is not None and outer.type == "object":
            prefix = _owner_symbol(outer, buffer)
            key_text = source_text(buffer, key)
            return f"{prefix}.{key_text}" if prefix else key_text
    return None


def _class_qualifier(class_node, buffer: bytes) -> str | None:
    owned = _owner_symbol(class_node, buffer)
    if owned:
        return owned
    name = class_node.child_by_field_name("name")
    if name is not None:
        return source_text(buffer, name)
    return None


def _method_symbol(node, buffer: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is None:
        return None
    method = source_text(buffer, name)
    parent = node.parent
    if parent is None:
        return method
    if parent.type == "class_body" and parent.parent is not None:
        prefix = _class_qualifier(parent.parent, buffer)
        if prefix:
            return f"{prefix}.{method}"
        line, column = location(buffer, parent.parent.start_byte)
        return f"class@{line}:{column}.{method}"
    if parent.type == "object":
        prefix = _owner_symbol(parent, buffer)
        if prefix:
            return f"{prefix}.{method}"
        line, column = location(buffer, parent.start_byte)
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
        return source_text(buffer, name)

    line, column = location(buffer, function_node.start_byte)
    return f"anonymous@{line}:{column}"


def _import_aliases(root, buffer: bytes) -> dict[str, str]:
    """``import {a as b}`` -> ``{"b": "a"}`` so a call through the local alias names the imported function."""
    aliases = {}
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type == "import_specifier":
            name, alias = node.child_by_field_name("name"), node.child_by_field_name("alias")
            if name is not None and alias is not None:
                aliases[source_text(buffer, alias)] = source_text(buffer, name)
        pending.extend(node.children)
    return aliases


def _preorder(root):
    """Every node under ``root`` (itself included) in document order, without recursion."""
    pending = [root]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node.children))


def _blind_text(function_node, buffer: bytes) -> str:
    """The function's source with every identifier leaf renamed by first appearance (see ``blind.py``)."""
    leaves = [(n.start_byte, n.end_byte) for n in _preorder(function_node) if n.type in _IDENTIFIER_LEAF_TYPES]
    return rename(buffer, function_node.start_byte, function_node.end_byte, leaves)


def collect_functions(source: str, path: Path) -> list[tuple[FuncRecord, str]]:
    """Extract executable JS/TS functions and exact UTF-8 source spans for clone analysis.

    The whole captured buffer is parsed and validated before any records are returned. This
    function does not execute source or load model weights; it only builds syntax fingerprints.
    """
    return [(record, snippet) for record, snippet, _ in collect_functions_with_blind(source, path)]


def collect_functions_with_blind(source: str, path: Path) -> list[tuple[FuncRecord, str, str]]:
    """``collect_functions`` rows plus each function's identifier-blind text as a third item."""
    buffer = source.encode("utf-8")
    parser, typescript = load_parser(path)
    tree, analysed = repair_jsx_ampersands(parser, buffer)
    root = tree.root_node
    issues = parse_issues(root)
    if root.has_error or issues:
        raise parse_failure(root, buffer, path, typescript, issues)

    functions = [n for n in _preorder(root) if n.type in FUNCTION_TYPES and n.child_by_field_name("body") is not None]
    functions.sort(key=lambda node: (node.start_byte, -node.end_byte))
    aliases = _import_aliases(root, analysed)
    records = []
    for node in functions:
        sizes: dict = {}
        tokens = runtime_body_tokens(node, analysed, sizes)
        token_tuple = tuple(tokens)
        record = FuncRecord(
            file_path=path,
            line=node.start_point.row + 1,
            symbol=_symbol(node, buffer),
            tokens=token_tuple,
            semantic=semantic_signature(token_tuple),
            language="javascript",
            column=location(buffer, node.start_byte)[1],
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            calls=_invocation_remainders(node, analysed, sizes, len(token_tuple), aliases),
        )
        snippet = buffer[node.start_byte : node.end_byte].decode("utf-8")
        records.append((record, snippet, _blind_text(node, buffer)))
    return records
