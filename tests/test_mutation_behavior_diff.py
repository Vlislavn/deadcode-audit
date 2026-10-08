"""Exercise mutation diff selection through real git and CLI processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.org")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "runtime").mkdir()
    (tmp_path / ".deadcode.yml").write_text("project:\n  source_roots: [runtime]\n")
    (tmp_path / "runtime" / "logic.py").write_text("def value(x: int = 1):\n    return 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "checkout", "-b", "change")
    return tmp_path


def cli(repo, command, compare="main"):
    return subprocess.run(
        [sys.executable, "-m", "deadcode_audit", command, "--compare-branch", compare],
        cwd=repo,
        env={**os.environ, "DEADCODE_REPO_ROOT": str(repo), "PYTHONPATH": str(SOURCE)},
        capture_output=True,
        text=True,
        check=False,
    )


def commit(repo, text, name="logic.py"):
    (repo / "runtime" / name).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")


def test_comment_only_skip_and_explicit_run_reason(repo):
    commit(repo, "# reviewed authorization\ndef value(x: int = 1):\n    return 1  # explanation\n")
    result = cli(repo, "mutation-targets")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    result = cli(repo, "run-mutmut-changed")
    assert result.returncode == 0, result.stderr
    assert "strictly AST-equivalent" in result.stdout
    assert cli(repo, "mypy-targets").stdout == "runtime/logic.py\n"


@pytest.mark.parametrize("text", [
    "def value(x: int = 1):\n    return 2\n",
    "def value(x: int = 2):\n    return 1\n",
    "def value(x: str = 1):\n    return 1\n",
    "@staticmethod\ndef value(x: int = 1):\n    return 1\n",
    'def value(x: int = 1):\n    """New documentation."""\n    return 1\n',
    "def renamed(x: int = 1):\n    return 1\n",
])
def test_ast_changes_selected(repo, text):
    commit(repo, text)
    result = cli(repo, "mutation-targets")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "runtime/logic.py\n"


def test_added_file_selected(repo):
    commit(repo, "# new file\n", "added.py")
    result = cli(repo, "mutation-targets")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "runtime/added.py\n"


@pytest.mark.parametrize("command", ["mutation-targets", "run-mutmut-changed"])
def test_invalid_compare_fails(repo, command):
    result = cli(repo, command, "missing-branch")
    assert result.returncode == 2
    assert "Unable to resolve compare branch" in result.stderr


def test_malformed_source_fails(repo):
    commit(repo, "def broken(:\n")
    result = cli(repo, "mutation-targets")
    assert result.returncode != 0
    assert "SyntaxError" in result.stderr


def test_no_changed_runtime_reason(repo):
    result = cli(repo, "run-mutmut-changed")
    assert result.returncode == 0, result.stderr
    assert "No mutation targets changed" in result.stdout
