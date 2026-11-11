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

from deadcode_audit import config, diffscope, javascript
from deadcode_audit.framework import Detector, run_detectors
from deadcode_audit.scoring import calculate_score

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


def _iter_source_files(targets: list[Path]) -> list[Path]:
    """Expand explicit targets to supported files without following outside aliases."""
    files: set[Path] = set()
    for target in targets:
        abs_target = (diffscope.REPO_ROOT / target).resolve()
        if not abs_target.is_relative_to(diffscope.REPO_ROOT) or not abs_target.exists():
            raise ValueError(f"Target missing or outside checkout: {target}")
        if abs_target.is_dir():
            files.update(p.relative_to(diffscope.REPO_ROOT) for p in abs_target.rglob("*")
                         if p.is_file() and p.suffix in SOURCE_SUFFIXES
                         and not {"node_modules", ".git", ".venv", "venv", "__pycache__", "mutants"} & set(p.relative_to(diffscope.REPO_ROOT).parts))
        elif abs_target.suffix in SOURCE_SUFFIXES:
            files.add(abs_target.relative_to(diffscope.REPO_ROOT))
    return sorted(files)


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


def run_scan(
    files: list[Path],
    detectors: Sequence[Detector],
    cfg: config.DeadcodeConfig,
) -> ScanResult:
    """Run all detectors over ``files``, apply config severities, and compute the score."""
    sources = [(path, diffscope.read_text(path)) for path in files]
    diagnostics = run_detectors(sources, detectors)
    return _result(diagnostics, len(files), cfg)


def _result(diagnostics, count, cfg, coverage=None) -> ScanResult:
    diagnostics = config.apply_rule_severities(diagnostics, cfg.rule_severity)
    score = calculate_score(
        diagnostics,
        weights=cfg.weights or None,
        smoothing=cfg.smoothing if cfg.smoothing is not None else 20,
        source_file_count=count,
        good_threshold=cfg.good_threshold if cfg.good_threshold is not None else 75,
        ok_threshold=cfg.ok_threshold if cfg.ok_threshold is not None else 50,
    )
    return ScanResult(diagnostics=diagnostics, score=score, file_count=count, coverage=coverage)


def run_source_scan(files: list[Path], detectors: Sequence[Detector], cfg: config.DeadcodeConfig) -> ScanResult:
    """Compose native Python detectors and the optional local JS/TS backend."""
    python = [path for path in files if path.suffix == ".py"]
    js = [path for path in files if path.suffix in javascript.SUFFIXES]
    result = run_scan(python, detectors, cfg)
    if not js:
        return result
    findings = [*result.diagnostics, *javascript.run(js)]
    return _result(findings, len(files), cfg, {"python": len(python), "javascript_typescript": len(js)})
