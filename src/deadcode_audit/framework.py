"""Detector framework: the per-file context, the detector protocol, and the runner.

A *detector* is any object exposing ``rules: tuple[RuleSpec, ...]`` and
``detect(ctx: FileContext) -> list[Diagnostic]``. The runner builds one :class:`FileContext`
per file (parsed once, masked once — shared across all detectors) and aggregates diagnostics.
A file that fails to parse is surfaced as an honest ``deadcode/parse-error`` diagnostic, never
silently skipped (SOURCE-D001 fail-fast: errors are signals, not swallowed).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from deadcode_audit import _mask, suppress

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
            self._masked.append(_mask.mask_strings_and_comments(self.source))
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
    from deadcode_audit import diffscope  # deferred: keeps the framework's import graph minimal

    return FileContext(
        path=path,
        source=source,
        tree=ast.parse(source),
        lines=tuple(source.splitlines()),
        is_test_file=diffscope.is_test_path(path),
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
        diagnostics.extend(suppress.apply_suppressions(file_diagnostics, source))
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
