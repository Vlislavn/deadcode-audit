"""The flat, self-describing finding contract shared by every detector (à la aislop ``types.ts``).

One denormalised record drives scoring, the three output formats, config severity overrides, and
fix routing. A small :class:`RuleSpec` registry lets ``rules`` list every rule and lets scoring
apply the style-weight, without a detector needing to look anything up at emission time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    """Diagnostic severity (drives the score penalty and the SARIF level)."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# Engine names group rules for output and carry the scoring weight (see scoring.py).
ENGINE_AI_SLOP = "ai-slop"
ENGINE_CODE_QUALITY = "code-quality"
ENGINE_SECURITY = "security"
ENGINE_REACHABILITY = "reachability"
ENGINE_REDUNDANCY = "redundancy"


@dataclass(frozen=True)
class RuleSpec:
    """Static metadata for one rule: its id, engine, default severity, and remediation help."""

    rule: str  # namespaced id, e.g. "ai-slop/swallowed-exception"
    engine: str
    default_severity: Severity
    category: str
    help: str
    style: bool = False  # style/maintainability rule -> half scoring weight
    fixable: bool = False


@dataclass(frozen=True)
class Diagnostic:
    """One finding. ``file_path`` is repo-relative POSIX; ``line``/``column`` are 1-based."""

    file_path: str
    engine: str
    rule: str
    severity: Severity
    message: str
    line: int
    column: int = 1
    help: str = ""
    category: str = ""
    fixable: bool = False
    detail: str | None = None


def diagnostic_from_spec(
    spec: RuleSpec,
    *,
    file_path: str,
    line: int,
    message: str,
    column: int = 1,
    detail: str | None = None,
    severity: Severity | None = None,
) -> Diagnostic:
    """Build a :class:`Diagnostic` from a :class:`RuleSpec`, inheriting its engine/category/help."""
    return Diagnostic(
        file_path=file_path,
        engine=spec.engine,
        rule=spec.rule,
        severity=severity or spec.default_severity,
        message=message,
        line=line,
        column=column,
        help=spec.help,
        category=spec.category,
        fixable=spec.fixable,
        detail=detail,
    )
