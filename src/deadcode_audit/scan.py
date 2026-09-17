"""Whole-codebase / diff-scoped scan: gather files, run detectors, score, render.

This is the unified surface aislop calls ``scan`` — one pass over a set of files producing a
``Diagnostic[]`` + a 0–100 score, rendered as terminal / JSON / SARIF / agent-prompt. Detectors
come from :mod:`deadcode_audit.detectors`; the existing reachability/redundancy tiers stay on
their own diff-scoped commands and are out of this AST-detector pass.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from deadcode_audit import config, diffscope
from deadcode_audit.framework import Detector, run_detectors
from deadcode_audit.scoring import (
    DEFAULT_SMOOTHING,
    GOOD_THRESHOLD,
    OK_THRESHOLD,
    calculate_score,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deadcode_audit.diagnostic import Diagnostic
    from deadcode_audit.scoring import ScoreResult


@dataclass(frozen=True)
class ScanResult:
    """Outcome of a scan: the diagnostics, the score, and the file count scanned."""

    diagnostics: list[Diagnostic]
    score: ScoreResult
    file_count: int


def _iter_python_files(targets: list[Path]) -> list[Path]:
    """Expand target paths (files or directories) to repo-relative Python files, deduplicated.

    Every returned path is repo-relative POSIX — a target written as an absolute path or with a
    ``./`` prefix is normalised the same way as a directory expansion, and a file reachable both
    explicitly and through a directory target is yielded once (a duplicate would double-penalise
    the score).
    """
    files: list[Path] = []
    seen: set[Path] = set()
    for target in targets:
        abs_target = (diffscope.REPO_ROOT / target).resolve()
        if not abs_target.is_relative_to(diffscope.REPO_ROOT) or not abs_target.exists():
            raise ValueError(f"Target missing or outside checkout: {target}")
        if abs_target.is_dir():
            candidates = (p.relative_to(diffscope.REPO_ROOT) for p in sorted(abs_target.rglob("*.py")))
        elif abs_target.suffix == ".py":
            candidates = (abs_target.relative_to(diffscope.REPO_ROOT),)
        else:
            continue  # non-Python file target: nothing to scan (unchanged behaviour)
        for rel in candidates:
            if rel not in seen:
                seen.add(rel)
                files.append(rel)
    return files


def resolve_target_files(
    paths: list[str],
    *,
    changed: bool,
    compare_branch: str,
    exclude: tuple[str, ...],
) -> list[Path]:
    """Resolve the file set to scan: changed src files, or the given paths (default ``src``)."""
    if changed:
        files = diffscope.changed_src_python_files(compare_branch)
    else:
        files = _iter_python_files([Path(p) for p in paths]) if paths else diffscope.runtime_files()
    if exclude:
        files = [f for f in files if not any(fnmatch.fnmatch(f.as_posix(), pat) for pat in exclude)]
    return files


def run_scan(
    files: list[Path],
    detectors: Sequence[Detector],
    cfg: config.DeadcodeConfig,
) -> ScanResult:
    """Run all detectors over ``files``, apply config severities, and compute the score.

    The half-weight style set is derived from the active detectors' ``RuleSpec.style`` flags —
    the documented contract — so a rule declared ``style=True`` is always scored as style,
    never via a stale hardcoded list.
    """
    sources = [(path, diffscope.read_text(path)) for path in files]
    diagnostics = run_detectors(sources, detectors)
    diagnostics = config.apply_rule_severities(diagnostics, cfg.rule_severity)
    style_rules = frozenset(spec.rule for detector in detectors for spec in detector.rules if spec.style)
    score = calculate_score(
        diagnostics,
        weights=cfg.weights or None,
        smoothing=cfg.smoothing if cfg.smoothing is not None else DEFAULT_SMOOTHING,
        source_file_count=len(files),
        good_threshold=cfg.good_threshold if cfg.good_threshold is not None else GOOD_THRESHOLD,
        ok_threshold=cfg.ok_threshold if cfg.ok_threshold is not None else OK_THRESHOLD,
        style_rules=style_rules,
    )
    return ScanResult(diagnostics=diagnostics, score=score, file_count=len(files))
