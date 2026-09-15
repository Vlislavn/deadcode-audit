"""Self-detection avoidance: mask string + comment content while preserving offsets.

Any *text/regex* detector should match against the masked source so that a pattern living
inside a string literal or comment (including a detector's own example text, or this module's
own docstrings) does not self-trigger. Masking replaces the *content* of string and comment
tokens with ``x`` while preserving every newline and the total length, so line/column numbers
computed against the masked text map 1:1 back onto the original. (AST-based detectors are
immune and do not need this.)
"""

from __future__ import annotations

import io
import token as token_mod
import tokenize


def mask_strings_and_comments(source: str) -> str:
    """Return ``source`` with string/comment token *contents* blanked, offsets preserved."""
    out = list(source)
    masked_types = {token_mod.STRING, token_mod.COMMENT}
    if hasattr(token_mod, "FSTRING_MIDDLE"):  # py312+ f-string token split
        masked_types.add(token_mod.FSTRING_MIDDLE)
    readline = io.StringIO(source).readline
    line_starts = _line_start_offsets(source)
    for tok in tokenize.generate_tokens(readline):
        if tok.type not in masked_types:
            continue
        start = line_starts[tok.start[0] - 1] + tok.start[1]
        end = line_starts[tok.end[0] - 1] + tok.end[1]
        for i in range(start, min(end, len(out))):
            if out[i] != "\n":
                out[i] = "x"
    return "".join(out)


def _line_start_offsets(source: str) -> list[int]:
    """Byte offset at which each 1-based line begins."""
    offsets = [0]
    for index, char in enumerate(source):
        if char == "\n":
            offsets.append(index + 1)
    return offsets
