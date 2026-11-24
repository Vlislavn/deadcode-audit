"""Tree-sitter loading, parse-error detection and the JSX ``&`` repair for the JS/TS function collector."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

PARSER_VERSION = "0.25.2"


def load_parser(path: Path):
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


def node_key(node) -> tuple[int, int, str]:
    return node.start_byte, node.end_byte, node.type


def source_text(buffer: bytes, node) -> str:
    return buffer[node.start_byte : node.end_byte].decode("utf-8")


def location(buffer: bytes, offset: int) -> tuple[int, int]:
    line_start = buffer.rfind(b"\n", 0, offset) + 1
    line = buffer.count(b"\n", 0, offset) + 1
    column = len(buffer[line_start:offset].decode("utf-8")) + 1
    return line, column


def parse_issues(root):
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


def repair_jsx_ampersands(parser, buffer: bytes):
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
            for issue in parse_issues(tree.root_node)
            if issue.is_error
            for token in issue.children
            if _is_stray_jsx_ampersand(token, data)
        ]
        if not fixes:
            return tree, bytes(data)
        for start, end in fixes:
            data[start:end] = b"_" * (end - start)


def parse_failure(
    root, buffer: bytes, path: Path, typescript: bool, issues
) -> SyntaxError:
    first = min(issues, key=lambda node: node.start_byte) if issues else root
    line, column = location(buffer, first.start_byte)
    language = "TypeScript" if typescript else "JavaScript"
    return SyntaxError(f"{path}:{line}:{column}: invalid {language} syntax")
