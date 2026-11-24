"""Identifier-blind function text for the overlaps second embedding signal (Type-2 clone preprocessing).

RULE: every identifier leaf of a function (locals, parameters, called/declared function names, property and
member names, shorthand properties; the function's own name) is renamed ``v0, v1, ...`` in order of first appearance,
one mapping per function, same text -> same name. Keywords, punctuation, string/number literals, comments and
whitespace stay as written. A function and a renamed copy of itself become identical; a function and a call site that
only shares the callee's *name* stop looking alike. JavaScript leaves come from Tree-sitter
(``javascript_functions``), Python leaves are the NAME tokens of ``tokenize`` (identifier = non-keyword NAME).
"""

from __future__ import annotations

import io
import keyword
import tokenize
from collections.abc import Iterable

BLIND_ID = "identifier-blind-v1"  # text-transform id: part of the embedding-cache key; bump when the rule above changes


def rename(buffer: bytes, start: int, end: int, spans: Iterable[tuple[int, int]]) -> str:
    """``buffer[start:end]`` with each identifier span (byte offsets, document order) renamed by first appearance."""
    names: dict[bytes, bytes] = {}
    out, cursor = [], start
    for first, last in spans:
        name = buffer[first:last]
        out.append(buffer[cursor:first])
        out.append(names.setdefault(name, b"v%d" % len(names)))
        cursor = last
    out.append(buffer[cursor:end])
    return b"".join(out).decode("utf-8")


def python_blind(source: str) -> str | None:
    """Identifier-blind text of one Python function segment, or ``None`` when it does not tokenize."""
    lines = io.StringIO(source).readlines()
    line_start = [0]
    for line in lines:
        line_start.append(line_start[-1] + len(line.encode("utf-8")))
    spans: list[tuple[int, int]] = []
    try:
        for token in tokenize.generate_tokens(iter(lines).__next__):
            if token.type == tokenize.NAME and not keyword.iskeyword(token.string):
                (row, col), (end_row, end_col) = token.start, token.end
                spans.append((line_start[row - 1] + len(lines[row - 1][:col].encode("utf-8")),
                              line_start[end_row - 1] + len(lines[end_row - 1][:end_col].encode("utf-8"))))
    except (tokenize.TokenError, SyntaxError):
        return None
    raw = source.encode("utf-8")
    return rename(raw, 0, len(raw), spans)
