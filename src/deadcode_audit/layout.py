"""Repository layout: the repo root, the configured source/test roots and the runtime-file walk.

``REPO_ROOT`` is read at call time (``layout.REPO_ROOT``), so a test that monkeypatches it is seen at every
use site. The git/diff primitives built on top of it live in :mod:`deadcode_audit.diffscope`.
"""

from __future__ import annotations

import os
from pathlib import Path

_env_root = os.environ.get("DEADCODE_REPO_ROOT")
REPO_ROOT: Path = Path(_env_root).resolve() if _env_root else Path.cwd().resolve()


def is_runtime_python_path(path: Path) -> bool:
    """Return True when a path belongs to runtime Python sources."""
    return path.suffix == ".py" and any(path.is_relative_to(root) for root in source_roots()) and not is_test_path(path)


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


def runtime_files(*, suffixes: frozenset[str] = frozenset({".py"})) -> list[Path]:
    excluded = {".git", ".venv", "venv", ".runtime", "node_modules", "__pycache__", "mutants"}
    result = set()
    for root in source_roots():
        full = REPO_ROOT / root
        if not full.exists():
            raise ValueError(f"Configured source root does not exist: {root}")
        paths = [full] if full.is_file() else full.rglob("*")
        for path in paths:
            rel = path.relative_to(REPO_ROOT)
            if path.is_file() and path.suffix in suffixes and not (set(rel.parts) & excluded) and not is_test_path(rel):
                result.add(rel)
    return sorted(result)
