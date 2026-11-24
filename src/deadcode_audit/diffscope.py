"""Shared git / diff-scope primitives for every dead-code tier (repo layout: :mod:`deadcode_audit.layout`).

This is the single source of the merge-base / changed-line computation that all
diff-scoped tiers (Vulture, redundancy, clones, runtime-consumer) use. It collapses
what used to be duplicated between ``dead_code_guard.py`` and ``redundancy_lint.py``
(two copies of ``changed_python_files`` / ``_added_lines_by_file`` reached via a
private cross-import) into one public surface.

Callers should reference these as *qualified* names (``diffscope.added_lines_by_file``)
so a test monkeypatching ``diffscope.<fn>`` is seen at every use site.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from deadcode_audit import layout

_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def _git_output(*args: str, strip: bool = True) -> str:
    """Return stdout for a git command executed from the repository root.

    Output is decoded as UTF-8 with replacement (``text=True`` alone would use the locale
    encoding, so a non-UTF-8 environment could raise ``UnicodeDecodeError`` mid-scan). Failures
    raise a :class:`RuntimeError` that carries git's stderr — the exit code alone does not say
    what went wrong.
    """
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=layout.REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 and not (args[0] == "grep" and completed.returncode == 1):
        raise RuntimeError(
            f"git {' '.join(args)} failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip() if strip else completed.stdout


def git_output(*args: str) -> str:
    """Read raw git output, preserving blob whitespace and NUL-delimited paths."""
    return _git_output(*args, strip=False)


def git_lines(*args: str) -> list[str]:
    """Return stdout lines for a git command executed from the repository root."""
    output = _git_output(*args)
    if not output:
        return []
    return output.splitlines()


def _resolve_compare_ref(compare_branch: str) -> str:
    """Resolve a local or remote compare reference."""
    candidates = [compare_branch, f"origin/{compare_branch}"]
    for candidate in candidates:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=layout.REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return candidate
    raise RuntimeError(f"Unable to resolve compare branch '{compare_branch}'")


def changed_files(compare_branch: str, *, suffixes: frozenset[str]) -> list[Path]:
    """Return changed tracked sources with the requested syntax suffixes."""
    compare_ref = _resolve_compare_ref(compare_branch)
    merge_base = _git_output("merge-base", "HEAD", compare_ref)
    diff_output = _git_output("diff", "--name-only", "--diff-filter=ACMR", f"{merge_base}..HEAD")
    files: list[Path] = []
    for line in diff_output.splitlines():
        path = Path(line)
        if path.suffix not in suffixes:
            continue
        if not (layout.REPO_ROOT / path).exists():
            continue
        files.append(path)
    return files


def changed_python_files(compare_branch: str) -> list[Path]:
    """Return changed tracked Python files relative to the compare branch."""
    return changed_files(compare_branch, suffixes=frozenset({".py"}))


def changed_src_python_files(compare_branch: str) -> list[Path]:
    """Return changed runtime ``src`` Python files relative to the compare branch."""
    return [path for path in changed_python_files(compare_branch) if layout.is_runtime_python_path(path)]


def parse_added_lines(diff_text: str) -> set[int]:
    """Return new-side line numbers added or modified in a ``git diff -U0`` patch."""
    added: set[int] = set()
    for raw_line in diff_text.splitlines():
        match = _DIFF_HUNK_RE.match(raw_line)
        if match is None:
            continue
        start = int(match.group("start"))
        count = 1 if match.group("count") is None else int(match.group("count"))
        added.update(range(start, start + count))
    return added


def parse_touched_lines(diff_text: str) -> set[int]:
    """Return new-side lines a ``git diff -U0`` patch touched, counting pure deletions.

    Like ``parse_added_lines`` but a zero-count hunk (lines removed, none added) marks the
    surrounding new-side lines ``start`` and ``start + 1`` so a deletion inside a function
    still touches it.
    """
    touched: set[int] = set()
    for raw_line in diff_text.splitlines():
        match = _DIFF_HUNK_RE.match(raw_line)
        if match is None:
            continue
        start = int(match.group("start"))
        count = 1 if match.group("count") is None else int(match.group("count"))
        touched.update(range(start, start + count) if count else (start, start + 1))
    return touched


def added_lines_by_file(compare_branch: str, paths: list[Path]) -> dict[str, set[int]]:
    """Map each changed file to the set of new-side lines it added or modified."""
    compare_ref = _resolve_compare_ref(compare_branch)
    merge_base = _git_output("merge-base", "HEAD", compare_ref)
    changed_lines_by_file: dict[str, set[int]] = {}
    for path in paths:
        diff_text = _git_output("diff", "-U0", f"{merge_base}..HEAD", "--", path.as_posix())
        changed_lines_by_file[path.as_posix()] = parse_added_lines(diff_text)
    return changed_lines_by_file
