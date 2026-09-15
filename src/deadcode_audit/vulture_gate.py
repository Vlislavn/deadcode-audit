"""Tier 2: diff-scoped Vulture reachability gate.

Runs Vulture across the whole ``src`` tree (so its usage graph is complete), then filters
findings to lines the branch added or modified. Blocks on high-confidence dead code and
advises on lower-confidence candidates.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from deadcode_audit import config, diffscope

_VULTURE_LINE_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<message>.+) \((?P<confidence>\d+)% confidence\)$")


@dataclass(frozen=True)
class VultureFinding:
    """Describe one Vulture dead-code report on a source line."""

    file_path: Path
    line: int
    message: str
    confidence: int


def parse_vulture_findings(output: str) -> list[VultureFinding]:
    """Parse Vulture stdout into structured findings, ignoring unrecognized lines."""
    findings: list[VultureFinding] = []
    for raw_line in output.splitlines():
        match = _VULTURE_LINE_RE.match(raw_line.strip())
        if match is None:
            continue
        findings.append(
            VultureFinding(
                file_path=Path(match.group("path")),
                line=int(match.group("line")),
                message=match.group("message"),
                confidence=int(match.group("confidence")),
            )
        )
    return findings


def filter_findings_to_changed_lines(
    findings: list[VultureFinding],
    changed_lines_by_file: dict[str, set[int]],
) -> list[VultureFinding]:
    """Keep findings whose definition line was added or modified on the branch."""
    kept: list[VultureFinding] = []
    for finding in findings:
        changed_lines = changed_lines_by_file.get(finding.file_path.as_posix())
        if changed_lines and finding.line in changed_lines:
            kept.append(finding)
    return kept


def split_blocking_advisory(
    findings: list[VultureFinding],
    block_confidence: int,
) -> tuple[list[VultureFinding], list[VultureFinding]]:
    """Partition findings into blocking (>= threshold) and advisory (below threshold)."""
    blocking = [finding for finding in findings if finding.confidence >= block_confidence]
    advisory = [finding for finding in findings if finding.confidence < block_confidence]
    return blocking, advisory


def _run_vulture(min_confidence: int) -> str:
    """Run Vulture across the source tree and return its stdout (raising on a tool error)."""
    whitelist = diffscope.project_paths("vulture_whitelist", [])
    for path in [*diffscope.source_roots(), *whitelist]:
        if not (diffscope.REPO_ROOT / path).exists():
            raise ValueError(f"Configured Vulture path does not exist: {path}")
    completed = subprocess.run(
        [
            sys.executable, "-m", "vulture",
            *(p.as_posix() for p in diffscope.source_roots()),
            *(p.as_posix() for p in whitelist),
            "--min-confidence",
            str(min_confidence),
            "--ignore-names",
            config.vulture_ignore_names_arg(),
            # Registration decorators (routes, MCP/LangChain tools, Chainlit callbacks) mark
            # framework-invoked symbols Vulture cannot see being called; ignoring them removes
            # that false-positive class without hiding plain-wrapper dead code.
            "--ignore-decorators",
            config.vulture_ignore_decorators_arg(),
        ],
        cwd=diffscope.REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # Vulture exits 0 (no dead code) or 3 (dead code found); anything else is a tool error.
    if completed.returncode not in (0, 3):
        raise RuntimeError(f"Vulture failed (exit {completed.returncode}): {completed.stderr.strip()}")
    # On 0/3 a per-file parse/read error (e.g. invalid syntax) still lands on stderr while the
    # exit code reflects the dead-code result. Surface it so silently-skipped files stay visible
    # rather than dropping out of the usage graph unnoticed.
    if completed.stderr.strip():
        print(
            f"[warning] Vulture could not analyze some files (results may be incomplete):\n{completed.stderr.strip()}",
            file=sys.stderr,
        )
    return completed.stdout


def _print_findings(findings: list[VultureFinding]) -> None:
    """Print Vulture findings using their original report format."""
    for finding in findings:
        print(f"  {finding.file_path.as_posix()}:{finding.line}: {finding.message} ({finding.confidence}% confidence)")


def vulture_changed_check(compare_branch: str) -> int:
    """Block on high-confidence dead code introduced on changed lines; advise on the rest."""
    changed_files = diffscope.changed_src_python_files(compare_branch)
    if not changed_files:
        print("No changed src files for Vulture reachability scan")
        return 0

    changed_lines_by_file = diffscope.added_lines_by_file(compare_branch, changed_files)
    findings = filter_findings_to_changed_lines(
        parse_vulture_findings(_run_vulture(config.VULTURE_ADVISORY_CONFIDENCE)),
        changed_lines_by_file,
    )
    blocking, advisory = split_blocking_advisory(findings, config.VULTURE_BLOCK_CONFIDENCE)

    if blocking:
        print(
            f"[block] {len(blocking)} dead-code finding(s) >= {config.VULTURE_BLOCK_CONFIDENCE}% "
            "confidence on changed lines:"
        )
        _print_findings(blocking)
    if advisory:
        print(
            f"[advisory, non-blocking] {len(advisory)} confidence-{config.VULTURE_ADVISORY_CONFIDENCE} "
            "dead-code candidate(s) on changed lines:"
        )
        _print_findings(advisory)
        print("  -> review, integrate, or add proven false positives to scripts/vulture_whitelist.py")

    if blocking:
        return 1
    print("Vulture reachability gate passed (no blocking dead code on changed lines)")
    return 0
