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


@dataclass(frozen=True)
class FileContext:
    """Everything a detector needs for one file, computed once and shared."""

    path: Path  # repo-relative
    source: str
    tree: ast.Module
    lines: tuple[str, ...]
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


def build_file_context(path: Path, source: str) -> FileContext:
    """Parse ``source`` into a shared :class:`FileContext` (raises ``SyntaxError`` on bad syntax)."""
    return FileContext(
        path=path,
        source=source,
        tree=ast.parse(source),
        lines=tuple(source.splitlines()),
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
        file_diagnostics: list[Diagnostic] = []
        for detector in detectors:
            file_diagnostics.extend(detector.detect(ctx))
        # Honour inline ``# ai-slop: ignore[...]`` / ``ignore-file`` directives (parse errors above
        # are NOT suppressible — an unparseable file is always surfaced).
        diagnostics.extend(suppress.apply_suppressions(file_diagnostics, source))
    return diagnostics


def all_rule_specs(detectors: Sequence[Detector]) -> list[RuleSpec]:
    """Collect every detector's RuleSpecs (plus the framework's parse-error rule), de-duplicated."""
    specs: dict[str, RuleSpec] = {PARSE_ERROR_RULE.rule: PARSE_ERROR_RULE}
    for detector in detectors:
        for spec in detector.rules:
            specs[spec.rule] = spec
    return sorted(specs.values(), key=lambda s: s.rule)
