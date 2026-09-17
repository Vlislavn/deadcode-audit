"""Inline suppression for the aislop scan: ``# ai-slop: ignore[rule]`` (ruff-noqa parity).

A scan finding is dropped when:
* its reported line carries ``# ai-slop: ignore`` (suppress every rule on that line) or
  ``# ai-slop: ignore[<code>, <code>]`` (suppress only those rules), or
* the file carries a file-level ``# ai-slop: ignore-file`` / ``# ai-slop: ignore-file[<code>]``
  (suppress matching rules anywhere — used for whole-file findings like ``file-too-large`` whose
  reported line is the module docstring and cannot host an inline directive).

A code matches a finding when it equals the full rule id OR its last path segment, so
``# ai-slop: ignore[swallowed-exception]`` and ``# ai-slop: ignore[ai-slop/swallowed-exception]``
both suppress ``ai-slop/swallowed-exception``. Directives are read via :mod:`tokenize` COMMENT
tokens, so a directive written inside a string/docstring can never self-suppress.

This is the documented escape hatch for AUTHORIZED code that matches a rule (a logged best-effort
boundary, a cohesive-by-design large module) — write the directive WITH a reason so intent is
explicit (SOURCE-D001: a fallback that is genuinely required is kept narrow and documented).
"""

from __future__ import annotations

import io
import re
import sys
import token as token_mod
import tokenize
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deadcode_audit.diagnostic import Diagnostic

_DIRECTIVE = re.compile(r"#\s*ai-slop:\s*(ignore-file|ignore)(?![\w-])(?:\s*\[([^\]]*)\])?", re.IGNORECASE)


@dataclass(frozen=True)
class Suppressions:
    """Parsed suppression directives for one file."""

    by_line: dict[int, frozenset[str] | None] = field(default_factory=dict)  # None => all rules on that line
    file_level: list[frozenset[str] | None] = field(default_factory=list)  # None => all rules file-wide

    def is_empty(self) -> bool:
        return not self.by_line and not self.file_level


def _parse_codes(group: str | None) -> frozenset[str] | None:
    """``None`` for a bare ``ignore`` (all rules); else the comma/space-separated code set.

    An explicitly empty bracket set (``ignore[]``) returns an empty frozenset, which matches no
    rule; the caller rejects it loudly so a typo cannot act as a silent no-op directive.
    """
    if group is None:
        return None
    return frozenset(code.strip() for code in re.split(r"[,\s]+", group) if code.strip())


def parse_suppressions(source: str) -> Suppressions:
    """Collect every ``# ai-slop: ignore[...]`` / ``ignore-file[...]`` directive (tokenised, not regex-on-source)."""
    suppressions = Suppressions()
    readline = io.StringIO(source).readline
    for tok in tokenize.generate_tokens(readline):
        if tok.type != token_mod.COMMENT:
            continue
        match = _DIRECTIVE.search(tok.string)
        if match is None:
            continue
        codes = _parse_codes(match.group(2))
        if codes is not None and not codes:
            print(
                f"warning: '{tok.string.strip()}' suppresses no rules (empty code set) — "
                "list codes like ignore[rule-name] or drop the brackets to suppress all rules on the line",
                file=sys.stderr,
            )
            continue
        if match.group(1).lower() == "ignore-file":
            suppressions.file_level.append(codes)
        else:
            suppressions.by_line[tok.start[0]] = codes
    return suppressions


def _matches(codes: frozenset[str] | None, rule: str) -> bool:
    """True when a directive's code set covers ``rule`` (bare ``ignore`` => all rules)."""
    if codes is None:
        return True
    return rule in codes or rule.split("/")[-1] in codes


def apply_suppressions(diagnostics: list[Diagnostic], source: str) -> list[Diagnostic]:
    """Drop diagnostics suppressed by a same-line or file-level ``# ai-slop: ignore`` directive."""
    suppressions = parse_suppressions(source)
    if suppressions.is_empty():
        return diagnostics
    kept: list[Diagnostic] = []
    for diag in diagnostics:
        if any(_matches(codes, diag.rule) for codes in suppressions.file_level):
            continue
        if diag.line in suppressions.by_line and _matches(suppressions.by_line[diag.line], diag.rule):
            continue
        kept.append(diag)
    return kept
