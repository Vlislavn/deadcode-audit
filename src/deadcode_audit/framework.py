"""Detector framework: the per-file context, the detector protocol, and the runner.

A *detector* is any object exposing ``rules: tuple[RuleSpec, ...]`` and
``detect(ctx: FileContext) -> list[Diagnostic]``. The runner builds one :class:`FileContext`
per file (parsed once, masked once — shared across all detectors) and aggregates diagnostics.
A file that fails to parse is surfaced as an honest ``deadcode/parse-error`` diagnostic, never
silently skipped (SOURCE-D001 fail-fast: errors are signals, not swallowed).
"""

from __future__ import annotations

import ast
import io
import re
import sys
import token as token_mod
import tokenize
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

if TYPE_CHECKING:
    from pathlib import Path
from deadcode_audit.diagnostic import (
    ENGINE_CODE_QUALITY,
    Diagnostic,
    RuleSpec,
    Severity,
)

PARSE_ERROR_RULE = RuleSpec(
    rule="deadcode/parse-error",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.ERROR,
    category="Parse",
    help="The file could not be parsed as Python; fix the syntax error so it can be analysed.",
)

DETECTOR_ERROR_RULE = RuleSpec(
    rule="deadcode/detector-error",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.ERROR,
    category="Parse",
    help="A detector crashed while analysing this file. The remaining findings are still reported; "
    "fix or report the detector bug instead of suppressing this diagnostic.",
)

# Defensive extras beyond ``SyntaxError`` for ``ast.parse``. Verified on CPython 3.13: null bytes
# and parser-stack-overflow nesting already surface as ``SyntaxError``; ``ValueError`` and
# ``RecursionError`` are the documented failure modes on older CPythons (kept defensively).
_PARSE_FAILURES: tuple[type[BaseException], ...] = (ValueError, RecursionError)


@dataclass(frozen=True)
class FileContext:
    """Everything a detector needs for one file, computed once and shared."""

    path: Path  # repo-relative
    source: str
    tree: ast.Module
    lines: tuple[str, ...]
    is_test_file: bool = False  # shared test-file verdict (config test_roots-aware) for detector sparing
    _masked: list[str] = field(default_factory=list, compare=False)

    @property
    def masked_source(self) -> str:
        """Source with string/comment contents blanked (offsets preserved); computed lazily once."""
        if not self._masked:
            self._masked.append(mask_strings_and_comments(self.source))
        return self._masked[0]


class Detector(Protocol):
    """A detector module/object: declares its rules and inspects one file context."""

    rules: tuple[RuleSpec, ...]

    def detect(self, ctx: FileContext) -> list[Diagnostic]: ...


def safe_parse(source: str) -> ast.Module | None:
    """Parse ``source``; return ``None`` for any unparseable input, never raise.

    Covers ``SyntaxError`` (bad syntax, and on CPython 3.12+ also BOMs, null bytes and parser
    overflow) plus ``ValueError``/``RecursionError`` — the equivalent failure modes on older
    CPythons. Callers that scan untrusted files use this so
    one malformed file can never crash the whole command; the scan runner turns the ``None`` into
    a visible ``deadcode/parse-error`` diagnostic (fail-fast as a signal, not a crash).
    """
    try:
        return ast.parse(source)
    except (SyntaxError, *_PARSE_FAILURES):
        return None


def build_file_context(path: Path, source: str) -> FileContext:
    """Parse ``source`` into a shared :class:`FileContext` (raises ``SyntaxError`` on bad syntax)."""
    # deferred: keeps the framework's import graph minimal
    from deadcode_audit import layout

    return FileContext(
        path=path,
        source=source,
        tree=ast.parse(source),
        lines=tuple(source.splitlines()),
        is_test_file=layout.is_test_path(path),
    )


def run_detectors(
    files: list[tuple[Path, str]],
    detectors: Sequence[Detector],
) -> list[Diagnostic]:
    """Run every detector over every (path, source); surface parse failures as diagnostics."""
    diagnostics: list[Diagnostic] = []
    for path, source in files:
        try:
            ctx = build_file_context(path, source)
        except SyntaxError as exc:  # surfaced, not swallowed — becomes a visible finding
            diagnostics.append(
                Diagnostic(
                    file_path=path.as_posix(),
                    engine=PARSE_ERROR_RULE.engine,
                    rule=PARSE_ERROR_RULE.rule,
                    severity=PARSE_ERROR_RULE.default_severity,
                    message=f"could not parse: {exc.msg}",
                    line=exc.lineno or 1,
                    help=PARSE_ERROR_RULE.help,
                    category=PARSE_ERROR_RULE.category,
                )
            )
            continue
        except _PARSE_FAILURES as exc:  # defensive: NUL bytes / deep nesting on older CPythons
            diagnostics.append(
                Diagnostic(
                    file_path=path.as_posix(),
                    engine=PARSE_ERROR_RULE.engine,
                    rule=PARSE_ERROR_RULE.rule,
                    severity=PARSE_ERROR_RULE.default_severity,
                    message=f"could not parse: {exc}",
                    line=1,
                    help=PARSE_ERROR_RULE.help,
                    category=PARSE_ERROR_RULE.category,
                )
            )
            continue
        file_diagnostics: list[Diagnostic] = []
        for detector in detectors:
            try:
                file_diagnostics.extend(detector.detect(ctx))
            except Exception as exc:  # a buggy detector must not void the run's other findings
                detector_name = getattr(detector, "__name__", type(detector).__name__).rsplit(".", 1)[-1]
                file_diagnostics.append(
                    Diagnostic(
                        file_path=path.as_posix(),
                        engine=DETECTOR_ERROR_RULE.engine,
                        rule=DETECTOR_ERROR_RULE.rule,
                        severity=DETECTOR_ERROR_RULE.default_severity,
                        message=f"detector {detector_name} failed: {type(exc).__name__}: {exc}",
                        line=1,
                        help=DETECTOR_ERROR_RULE.help,
                        category=DETECTOR_ERROR_RULE.category,
                    )
                )
        # Honour inline ``# ai-slop: ignore[...]`` / ``ignore-file`` directives (parse errors above
        # are NOT suppressible — an unparseable file is always surfaced).
        diagnostics.extend(apply_suppressions(file_diagnostics, source))
    return diagnostics


def all_rule_specs(detectors: Sequence[Detector]) -> list[RuleSpec]:
    """Collect every detector's RuleSpecs (plus the framework's parse-error rule), de-duplicated."""
    specs: dict[str, RuleSpec] = {
        PARSE_ERROR_RULE.rule: PARSE_ERROR_RULE,
        DETECTOR_ERROR_RULE.rule: DETECTOR_ERROR_RULE,
    }
    for detector in detectors:
        for spec in detector.rules:
            specs[spec.rule] = spec
    return sorted(specs.values(), key=lambda s: s.rule)


# ====================================================================================================
# Inline suppression (formerly the ``suppress`` module)
# ----------------------------------------------------------------------------------------------------
# Inline suppression for the aislop scan: ``# ai-slop: ignore[rule]`` (ruff-noqa parity).
#
# A scan finding is dropped when:
# * its reported line carries ``# ai-slop: ignore`` (suppress every rule on that line) or
#   ``# ai-slop: ignore[<code>, <code>]`` (suppress only those rules), or
# * the file carries a file-level ``# ai-slop: ignore-file`` / ``# ai-slop: ignore-file[<code>]``
#   (suppress matching rules anywhere — used for whole-file findings like ``file-too-large`` whose
#   reported line is the module docstring and cannot host an inline directive).
#
# A code matches a finding when it equals the full rule id OR its last path segment, so
# ``# ai-slop: ignore[swallowed-exception]`` and ``# ai-slop: ignore[ai-slop/swallowed-exception]``
# both suppress ``ai-slop/swallowed-exception``. Directives are read via :mod:`tokenize` COMMENT
# tokens, so a directive written inside a string/docstring can never self-suppress.
#
# This is the documented escape hatch for AUTHORIZED code that matches a rule (a logged best-effort
# boundary, a cohesive-by-design large module) — write the directive WITH a reason so intent is
# explicit (SOURCE-D001: a fallback that is genuinely required is kept narrow and documented).
# ====================================================================================================


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


# ====================================================================================================
# Source masking (formerly the ``_mask`` module)
# ----------------------------------------------------------------------------------------------------
# Self-detection avoidance: mask string + comment content while preserving offsets.
#
# Any *text/regex* detector should match against the masked source so that a pattern living
# inside a string literal or comment (including a detector's own example text, or this module's
# own docstrings) does not self-trigger. Masking replaces the *content* of string and comment
# tokens with ``x`` while preserving every newline and the total length, so line/column numbers
# computed against the masked text map 1:1 back onto the original. (AST-based detectors are
# immune and do not need this.)
# ====================================================================================================


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
