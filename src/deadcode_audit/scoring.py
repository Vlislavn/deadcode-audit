"""0–100 code-health score (faithful port of aislop's ``scoring/index.ts``).

Per-diagnostic penalty = severity_penalty × engine_weight × style_factor, summed, then
density-normalised (``sqrt`` of issue density with an additive smoothing constant so a few
issues in a large repo barely move the score) and passed through a self-normalising
logarithmic curve so the first issues cost the most and the score asymptotes toward 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from deadcode_audit.diagnostic import Diagnostic, Severity

SEVERITY_PENALTY: dict[Severity, float] = {
    Severity.ERROR: 3.0,
    Severity.WARNING: 1.0,
    Severity.INFO: 0.25,
}

# Engine weights: AI-slop dominates so genuine slop drives the score, not house style.
DEFAULT_WEIGHTS: dict[str, float] = {
    "ai-slop": 2.5,
    "security": 1.5,
    "code-quality": 0.8,
    "reachability": 0.8,
    "redundancy": 0.8,
    "lint": 0.6,
    "format": 0.3,
}

# Style/maintainability rules contribute HALF weight (genuine slop, not house style, drives the number).
STYLE_RULES: frozenset[str] = frozenset(
    {
        "ai-slop/trivial-comment",
        "ai-slop/narrative-comment",
        "code-quality/file-too-large",
        "code-quality/function-too-long",
    }
)

DEFAULT_SMOOTHING = 20
GOOD_THRESHOLD = 75
OK_THRESHOLD = 50


@dataclass(frozen=True)
class ScoreResult:
    """The computed score and its human label."""

    score: int
    label: str


def _label(score: int, good: int, ok: int) -> str:
    if score >= good:
        return "Healthy"
    if score >= ok:
        return "Needs Work"
    return "Critical"


def calculate_score(
    diagnostics: list[Diagnostic],
    *,
    weights: dict[str, float] | None = None,
    smoothing: int = DEFAULT_SMOOTHING,
    source_file_count: int | None = None,
    good_threshold: int = GOOD_THRESHOLD,
    ok_threshold: int = OK_THRESHOLD,
) -> ScoreResult:
    """Return the 0–100 score + label for a set of diagnostics."""
    if not diagnostics:
        return ScoreResult(score=100, label="Healthy")

    weight_map = weights or DEFAULT_WEIGHTS
    deductions = 0.0
    for diag in diagnostics:
        penalty = SEVERITY_PENALTY.get(diag.severity, 1.0)
        engine_weight = weight_map.get(diag.engine, 1.0)
        style_factor = 0.5 if diag.rule in STYLE_RULES else 1.0
        deductions += penalty * engine_weight * style_factor

    distinct_files = len({diag.file_path for diag in diagnostics})
    effective_files = source_file_count if source_file_count is not None else max(distinct_files, 1)
    issue_density = min(1.0, len(diagnostics) / (effective_files + smoothing))
    scaled = deductions * math.sqrt(issue_density)

    score = max(0, round(100 - 100 * math.log1p(scaled) / math.log1p(100 + scaled)))
    return ScoreResult(score=score, label=_label(score, good_threshold, ok_threshold))
