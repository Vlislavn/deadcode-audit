"""Real CLI: a killed mutant and a broken clean baseline must be distinguished."""
import os
import subprocess
import sys

import pytest

pytest.importorskip("mutmut")
pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="mutmut requires POSIX fork")


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.mark.parametrize("broken_baseline", [False, True])
def test_mutation_cli_never_accepts_broken_baseline(tmp_path, broken_baseline):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fixture"\nversion="0.0.0"\n'
        '[tool.pytest.ini_options]\npythonpath=["src"]\n'
    )
    (tmp_path / ".deadcode.yml").write_text(
        'project:\n  source_roots: [src]\n  test_roots: [tests]\n'
        '  mutation_roots: [src]\n  mutation_also_copy: [tests]\n'
    )
    source = tmp_path / "src/example.py"
    source.write_text("def add(a, b):\n    return a\n")
    expected = 0 if broken_baseline else 5
    (tmp_path / "tests/test_example.py").write_text(
        f"from example import add\ndef test_add():\n    assert add(2,3) == {expected}\n"
    )
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "before")
    git(tmp_path, "switch", "-qc", "fix")
    source.write_text("def add(a, b):\n    return a + b\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "after")
    result = subprocess.run(
        [sys.executable, "-m", "deadcode_audit", "run-mutmut-changed", "--compare-branch", "main"],
        cwd=tmp_path,
        env=dict(os.environ, DEADCODE_REPO_ROOT=str(tmp_path), DEADCODE_MUTMUT_FRAGILE_LOG=str(tmp_path / "fragile.log")),
        capture_output=True, text=True, timeout=45,
    )
    if broken_baseline:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "100.0%" not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "killed=1, survived=0" in result.stdout
