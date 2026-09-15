"""Shared git / diff-scope primitives for every dead-code tier.

This is the single source of the merge-base / changed-line computation that all
diff-scoped tiers (Vulture, redundancy, clones, runtime-consumer) use. It collapses
what used to be duplicated between ``dead_code_guard.py`` and ``redundancy_lint.py``
(two copies of ``changed_python_files`` / ``_added_lines_by_file`` reached via a
private cross-import) into one public surface.

Callers should reference these as *qualified* names (``diffscope.added_lines_by_file``)
so a test monkeypatching ``diffscope.<fn>`` is seen at every use site.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_env_root = os.environ.get("DEADCODE_REPO_ROOT")
REPO_ROOT: Path = Path(_env_root).resolve() if _env_root else Path.cwd().resolve()

_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def _git_output(*args: str) -> str:
    """Return stdout for a git command executed from the repository root."""
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 and not (args[0] == "grep" and completed.returncode == 1):
        completed.check_returncode()
    return completed.stdout.strip()


def _git_lines(*args: str) -> list[str]:
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
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return candidate
    raise RuntimeError(f"Unable to resolve compare branch '{compare_branch}'")


def is_runtime_python_path(path: Path) -> bool:
    """Return True when a path belongs to runtime Python sources."""
    return path.suffix == ".py" and any(path.is_relative_to(root) for root in source_roots()) and not is_test_path(path)


def changed_python_files(compare_branch: str) -> list[Path]:
    """Return changed tracked Python files relative to the compare branch."""
    compare_ref = _resolve_compare_ref(compare_branch)
    merge_base = _git_output("merge-base", "HEAD", compare_ref)
    diff_output = _git_output("diff", "--name-only", "--diff-filter=ACMR", f"{merge_base}..HEAD")
    files: list[Path] = []
    for line in diff_output.splitlines():
        path = Path(line)
        if path.suffix != ".py":
            continue
        if not (REPO_ROOT / path).exists():
            continue
        files.append(path)
    return files


def changed_src_python_files(compare_branch: str) -> list[Path]:
    """Return changed runtime ``src`` Python files relative to the compare branch."""
    return [path for path in changed_python_files(compare_branch) if is_runtime_python_path(path)]


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


def added_lines_by_file(compare_branch: str, paths: list[Path]) -> dict[str, set[int]]:
    """Map each changed file to the set of new-side lines it added or modified."""
    compare_ref = _resolve_compare_ref(compare_branch)
    merge_base = _git_output("merge-base", "HEAD", compare_ref)
    changed_lines_by_file: dict[str, set[int]] = {}
    for path in paths:
        diff_text = _git_output("diff", "-U0", f"{merge_base}..HEAD", "--", path.as_posix())
        changed_lines_by_file[path.as_posix()] = parse_added_lines(diff_text)
    return changed_lines_by_file


def all_src_files() -> list[Path]:
    """Return every ``src`` Python file relative to the repo root (whole-corpus scans)."""
    return runtime_files()


def read_text(path: Path) -> str:
    """Read a repo-relative file as UTF-8."""
    return (REPO_ROOT / path).read_text(encoding="utf-8-sig")


def project_paths(key: str, defaults: list[Path]) -> list[Path]:
    from deadcode_audit.config import load_deadcode_config
    values = load_deadcode_config(REPO_ROOT).project.get(key)
    return [Path(v) for v in values] if values is not None else defaults


def source_roots() -> list[Path]:
    defaults = [Path(p) for p in ("src", "scripts") if (REPO_ROOT / p).is_dir()]
    return project_paths("source_roots", defaults or [Path(".")])


def is_test_path(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in project_paths("test_roots", [Path("tests")])) or "tests" in path.parts or path.name.startswith("test_")


def runtime_files() -> list[Path]:
    excluded = {".git", ".venv", "venv", ".runtime", "node_modules", "__pycache__", "mutants"}
    result = set()
    for root in source_roots():
        full = REPO_ROOT / root
        if not full.exists():
            raise ValueError(f"Configured source root does not exist: {root}")
        paths = [full] if full.is_file() else full.rglob("*.py")
        for path in paths:
            rel = path.relative_to(REPO_ROOT)
            if not (set(rel.parts) & excluded) and is_runtime_python_path(rel):
                result.add(rel)
    return sorted(result)
