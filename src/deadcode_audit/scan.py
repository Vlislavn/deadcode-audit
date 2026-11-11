"""Whole-codebase / diff-scoped scan: gather files, run detectors, score, render.

This is the unified surface aislop calls ``scan`` — one pass over a set of files producing a
``Diagnostic[]`` + a 0–100 score, rendered as terminal / JSON / SARIF / agent-prompt. Detectors
come from :mod:`deadcode_audit.detectors`, plus the whole-tree ``reachability/dead-symbol`` rule
(:func:`deadcode_audit.reachability_scan.dead_symbol_diagnostics`) so ``ci`` gates on unreferenced symbols.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from deadcode_audit import config, diffscope, javascript, reachability_scan
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
    coverage: dict[str, int] | None = None


SOURCE_SUFFIXES = javascript.SUFFIXES | {".py"}
_SKIPPED_DIRS = frozenset({"node_modules", ".git", ".venv", "venv", "__pycache__", "mutants"})


def _iter_source_files(targets: list[Path]) -> list[Path]:
    """Expand target paths (files or directories) to repo-relative supported sources, deduplicated.

    Every returned path is repo-relative POSIX — a target written as an absolute path or with a
    ``./`` prefix is normalised the same way as a directory expansion, and a file reachable both
    explicitly and through a directory target is yielded once (a duplicate would double-penalise
    the score). Directory expansion skips vendored/virtualenv/cache directories.
    """
    files: list[Path] = []
    seen: set[Path] = set()
    for target in targets:
        abs_target = (diffscope.REPO_ROOT / target).resolve()
        if not abs_target.is_relative_to(diffscope.REPO_ROOT) or not abs_target.exists():
            raise ValueError(f"Target missing or outside checkout: {target}")
        if abs_target.is_dir():
            relative = (p.relative_to(diffscope.REPO_ROOT) for p in sorted(abs_target.rglob("*")) if p.is_file())
            candidates = (p for p in relative if p.suffix in SOURCE_SUFFIXES and not _SKIPPED_DIRS & set(p.parts))
        elif abs_target.suffix in SOURCE_SUFFIXES:
            candidates = (abs_target.relative_to(diffscope.REPO_ROOT),)
        else:
            continue  # unsupported file target: nothing to scan (unchanged behaviour)
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
    """Resolve scan/CI sources; other Python-only commands retain their discovery."""
    if changed:
        files = [path for path in diffscope.changed_files(compare_branch, suffixes=SOURCE_SUFFIXES)
                 if any(path.is_relative_to(root) for root in diffscope.source_roots())
                 and not diffscope.is_test_path(path)]
    else:
        files = _iter_source_files([Path(p) for p in paths]) if paths else diffscope.runtime_files(suffixes=SOURCE_SUFFIXES)
    if exclude:
        files = [f for f in files if not any(fnmatch.fnmatch(f.as_posix(), pat) for pat in exclude)]
    return files


def _python_diagnostics(files: list[Path], detectors: Sequence[Detector], dead_symbols: bool) -> list[Diagnostic]:
    sources = [(path, diffscope.read_text(path)) for path in files]
    diagnostics = run_detectors(sources, detectors)
    if dead_symbols:
        diagnostics += reachability_scan.dead_symbol_diagnostics(files)
    return diagnostics


def _result(
    diagnostics: list[Diagnostic],
    count: int,
    cfg: config.DeadcodeConfig,
    detectors: Sequence[Detector],
    coverage: dict[str, int] | None = None,
) -> ScanResult:
    """Apply config severities and score ``diagnostics`` over ``count`` scanned files."""
    diagnostics = config.apply_rule_severities(diagnostics, cfg.rule_severity)
    style_rules = frozenset(spec.rule for detector in detectors for spec in detector.rules if spec.style)
    score = calculate_score(
        diagnostics,
        weights=cfg.weights or None,
        smoothing=cfg.smoothing if cfg.smoothing is not None else DEFAULT_SMOOTHING,
        source_file_count=count,
        good_threshold=cfg.good_threshold if cfg.good_threshold is not None else GOOD_THRESHOLD,
        ok_threshold=cfg.ok_threshold if cfg.ok_threshold is not None else OK_THRESHOLD,
        style_rules=style_rules,
    )
    return ScanResult(diagnostics=diagnostics, score=score, file_count=count, coverage=coverage)


def run_scan(
    files: list[Path],
    detectors: Sequence[Detector],
    cfg: config.DeadcodeConfig,
    *,
    dead_symbols: bool = True,
) -> ScanResult:
    """Run all detectors over Python ``files``, apply config severities, and compute the score.

    The half-weight style set is derived from the active detectors' ``RuleSpec.style`` flags —
    the documented contract — so a rule declared ``style=True`` is always scored as style,
    never via a stale hardcoded list.
    """
    return _result(_python_diagnostics(files, detectors, dead_symbols), len(files), cfg, detectors)


def run_source_scan(files: list[Path], detectors: Sequence[Detector], cfg: config.DeadcodeConfig) -> ScanResult:
    """Compose native Python detectors and the optional local JS/TS backend."""
    python = [path for path in files if path.suffix == ".py"]
    js = [path for path in files if path.suffix in javascript.SUFFIXES]
    if not js:
        return run_scan(python, detectors, cfg)
    findings = [*_python_diagnostics(python, detectors, True), *javascript.run(js)]
    return _result(findings, len(files), cfg, detectors, {"python": len(python), "javascript_typescript": len(js)})
