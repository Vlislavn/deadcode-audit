"""Security gates exercised in isolated real checkout fixtures."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run(root, *args):
    env = dict(os.environ, DEADCODE_REPO_ROOT=str(root))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "deadcode_audit", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            '"""except Exception: pass; eval(value); importlib.import_module(name)"""\n',
            0,
        ),
        (
            'def recover():\n    try:\n        work()\n    except Exception:\n        return "unavailable"\n',
            0,
        ),
        ("try:\n    work()\nexcept Exception:\n    pass\n", 1),
        ("eval(value)\n", 1),
        ("importlib.import_module(name)\n", 1),
        (
            "importlib.import_module(name)  # ai-slop: ignore[security/dynamic-import] - trusted plugin registry\n",
            0,
        ),
        (
            "eval(value)  # ai-slop: ignore[security/dynamic-import] - unrelated rule\n",
            1,
        ),
        ("def (:\n", 1),
    ],
)
def test_security_command_gates_actual_ast_errors_with_specific_annotations(
    tmp_path, source, expected
):
    path = tmp_path / "boundary.py"
    path.write_text(source, encoding="utf-8")
    # Security never consumes general deadcode config severity overrides.
    (tmp_path / ".deadcode.yml").write_text(
        "rules:\n  security/eval: off\n", encoding="utf-8"
    )
    assert _run(tmp_path, "security", str(path)).returncode == expected


def test_security_command_rejects_missing_targets(tmp_path):
    result = _run(tmp_path, "security", str(tmp_path / "missing.py"))
    assert result.returncode == 1
    assert result.stderr == "Security scan target does not exist\n"


def test_security_command_rejects_empty_directory(tmp_path):
    result = _run(tmp_path, "security", str(tmp_path))
    assert result.returncode == 1
    assert result.stderr == "Security scan found no Python files\n"


def test_security_uses_configured_roots_without_severity_disabling(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "boundary.py").write_text("eval(value)\n", encoding="utf-8")
    (tmp_path / ".deadcode.yml").write_text(
        "project:\n  source_roots: [app]\nrules:\n  security/eval: off\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, "security")
    assert result.returncode == 1
    assert "security/eval" in result.stdout


def test_security_retains_operational_failure_envelope(tmp_path):
    outside = tmp_path.parent / "outside-security.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    result = _run(tmp_path, "security", str(outside))
    assert result.returncode == 2
    assert "outside checkout" in result.stderr
    assert "Traceback" not in result.stderr
