"""Render diagnostics to the three aislop-style surfaces: terminal, JSON, SARIF 2.1.0.

Plus an agent hand-off prompt (the cheap half of aislop's two-track fix — we adopt the
hand-off, defer the AST auto-writer). All renderers consume the same ``Diagnostic[]``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from deadcode_audit.diagnostic import Diagnostic, Severity

if TYPE_CHECKING:
    from deadcode_audit.scoring import ScoreResult

SARIF_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "note"}
_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def _summary(diagnostics: list[Diagnostic]) -> dict[str, int]:
    return {
        "errors": sum(1 for d in diagnostics if d.severity is Severity.ERROR),
        "warnings": sum(1 for d in diagnostics if d.severity is Severity.WARNING),
        "info": sum(1 for d in diagnostics if d.severity is Severity.INFO),
        "files": len({d.file_path for d in diagnostics}),
        "total": len(diagnostics),
    }


def render_json(diagnostics: list[Diagnostic], score: ScoreResult, *, files_scanned: int | None = None) -> str:
    """Machine envelope: score, label, per-engine counts, summary, and every diagnostic."""
    engines: dict[str, int] = {}
    for diag in diagnostics:
        engines[diag.engine] = engines.get(diag.engine, 0) + 1
    payload = {
        "schemaVersion": "1",
        "files_scanned": files_scanned,
        "score": score.score,
        "label": score.label,
        "engines": engines,
        "summary": _summary(diagnostics),
        "diagnostics": [
            {
                "filePath": d.file_path,
                "engine": d.engine,
                "rule": d.rule,
                "severity": d.severity.value,
                "message": d.message,
                "line": d.line,
                "column": d.column,
                "help": d.help,
                "category": d.category,
                "fixable": d.fixable,
                "detail": d.detail,
            }
            for d in sorted(diagnostics, key=lambda d: (d.file_path, d.line, d.rule))
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def render_sarif(diagnostics: list[Diagnostic]) -> str:
    """SARIF 2.1.0 with a de-duplicated rule descriptor table referenced by ``ruleIndex``."""
    rule_index: dict[str, int] = {}
    rules: list[dict[str, object]] = []
    for diag in diagnostics:
        if diag.rule not in rule_index:
            rule_index[diag.rule] = len(rules)
            descriptor: dict[str, object] = {
                "id": diag.rule,
                "name": diag.rule,
                # Rule-level description (identical for every instance of the rule), not the
                # first diagnostic's instance message — SARIF viewers show this as the rule summary.
                "shortDescription": {"text": diag.help or diag.message},
            }
            if diag.help:  # remediation guidance; helpUri is omitted (no stable docs URL exists)
                descriptor["help"] = {"text": diag.help}
            rules.append(descriptor)
    results = [
        {
            "ruleId": diag.rule,
            "ruleIndex": rule_index[diag.rule],
            "level": SARIF_LEVEL[diag.severity],
            "message": {"text": diag.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": diag.file_path},
                        "region": {"startLine": max(1, diag.line), "startColumn": max(1, diag.column)},
                    }
                }
            ],
        }
        for diag in sorted(diagnostics, key=lambda d: (d.file_path, d.line, d.rule))
    ]
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "deadcode-audit", "rules": rules}},
                "results": results,
            }
        ],
    }
    return json.dumps(sarif, indent=2)


def render_terminal(diagnostics: list[Diagnostic], score: ScoreResult) -> str:
    """Human view: score header, then findings grouped by engine, severity-sorted."""
    summary = _summary(diagnostics)
    lines = [
        f"deadcode score: {score.score}/100 ({score.label})",
        f"  {summary['errors']} error(s), {summary['warnings']} warning(s), {summary['info']} info "
        f"across {summary['files']} file(s)",
    ]
    if not diagnostics:
        lines.append("  no findings")
        return "\n".join(lines)
    ordered = sorted(diagnostics, key=lambda d: (d.engine, _SEVERITY_ORDER[d.severity], d.file_path, d.line))
    current_engine = None
    for diag in ordered:
        if diag.engine != current_engine:
            current_engine = diag.engine
            lines.append(f"\n[{diag.engine}]")
        detail = f" ({diag.detail})" if diag.detail else ""
        lines.append(f"  {diag.severity.value:<7} {diag.file_path}:{diag.line} {diag.rule} — {diag.message}{detail}")
    return "\n".join(lines)


def render_agent_prompt(diagnostics: list[Diagnostic]) -> str:
    """Markdown hand-off so a coding agent can fix the findings with full context."""
    if not diagnostics:
        return "No deadcode/slop findings to fix."
    lines = [
        "# Fix the following dead-code / AI-slop findings",
        "",
        "Each item is `file:line rule — message`. Apply the minimal, behavior-preserving fix; "
        "if a finding is a false positive, say why rather than suppressing it blindly.",
        "",
    ]
    for diag in sorted(diagnostics, key=lambda d: (d.file_path, d.line)):
        lines.append(f"- `{diag.file_path}:{diag.line}` **{diag.rule}** — {diag.message}")
        if diag.help:
            lines.append(f"    - how: {diag.help}")
    return "\n".join(lines)
