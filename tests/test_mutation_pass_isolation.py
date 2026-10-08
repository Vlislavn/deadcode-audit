"""Native pytest passes: private scratch trees and bounded loop failures."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mutmut")
pytest.importorskip("pytest_timeout")
SUBPROCESS_SAFETY_CAP_SECONDS = 60


def run_native(root, code, **env):
    return subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        env={**os.environ, "DEADCODE_REPO_ROOT": str(root), **env},
        text=True, capture_output=True, timeout=SUBPROCESS_SAFETY_CAP_SECONDS, check=False,
    )


@pytest.fixture
def mini_repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths=["src"]\n')
    (tmp_path / "src").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


SETUP = '''
from pathlib import Path
import os
from deadcode_audit.mutation import _configure_mutation_for_paths, _patch_pass_isolation
from mutmut.__main__ import PytestRunner
_configure_mutation_for_paths([Path("src/example.py")])
_patch_pass_isolation()
runner = PytestRunner()
'''


def test_parent_passes_and_fork_children_have_private_cleaned_temps(mini_repo):
    (mini_repo / "tests/test_probe.py").write_text('''
from pathlib import Path
import os

def test_probe(tmp_path):
    with Path("receipts").open("a") as out:
        out.write(f"{os.getpid()} {tmp_path}\\n")
    assert "pytest-current" not in str(tmp_path)
''')
    result = run_native(mini_repo, SETUP + '''
runner._pytest_add_cli_args += ["--basetemp=local-shared-pytest-current"]
assert runner.execute_pytest(["tests", "-q"]) == 0
assert runner.execute_pytest(["tests", "-q"]) == 0
pid = os.fork()
if pid == 0:
    os._exit(runner.execute_pytest(["tests", "-q"]))
assert os.waitpid(pid, 0)[1] == 0
''')
    assert result.returncode == 0, result.stdout + result.stderr
    receipts = [line.split() for line in (mini_repo / "receipts").read_text().splitlines()]
    assert len(receipts) == 3
    assert receipts[0][0] == receipts[1][0] != receipts[2][0]
    bases = [Path(path).parents[1] for _, path in receipts]
    assert len(set(bases)) == 3
    assert all(base.name.startswith(f"deadcode-mutmut-{pid}-") for (pid, _), base in zip(receipts, bases))
    assert all(not base.exists() for base in bases)
    assert not (mini_repo / "local-shared-pytest-current").exists()


def test_infinite_loop_is_native_pytest_failure_not_harness_timeout(mini_repo):
    (mini_repo / "src/example.py").write_text('def resolve():\n    while True:\n        pass\n')
    (mini_repo / "tests/test_loop.py").write_text('from src.example import resolve\ndef test_resolve():\n    resolve()\n')
    result = run_native(mini_repo, SETUP + 'from mutmut.__main__ import status_by_exit_code\ncode = runner.execute_pytest(["tests", "-q"])\nassert code == 1 and status_by_exit_code[code] == "killed"\n',
                        DEADCODE_MUTMUT_TEST_TIMEOUT_SECONDS="0.2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Timeout" in result.stdout


def test_failed_generation_baseline_cleans_temp(mini_repo):
    (mini_repo / "tests/test_fail.py").write_text('from pathlib import Path\ndef test_fail(tmp_path):\n    Path("receipt").write_text(str(tmp_path))\n    assert False\n')
    result = run_native(mini_repo, SETUP + 'runner.execute_pytest(["tests", "-q"])\n',
                        MUTANT_UNDER_TEST="mutant_generation")
    assert result.returncode == 1, result.stdout + result.stderr
    assert not Path((mini_repo / "receipt").read_text()).parents[1].exists()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "61", "bad"])
def test_invalid_budget_rejected_before_pytest(mini_repo, value):
    result = run_native(mini_repo, SETUP, DEADCODE_MUTMUT_TEST_TIMEOUT_SECONDS=value)
    assert result.returncode != 0
    assert "ValueError" in result.stderr


def test_explicit_native_mutation_timeout_is_respected(mini_repo):
    (mini_repo / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths=["src"]\npytest_add_cli_args=["--timeout=12"]\n'
    )
    (mini_repo / "tests/test_budget.py").write_text('def test_budget(pytestconfig):\n    assert pytestconfig._env_timeout == 12\n')
    result = run_native(mini_repo, SETUP + 'assert runner.execute_pytest(["tests", "-q"]) == 0\n')
    assert result.returncode == 0, result.stdout + result.stderr


def test_env_budget_overrides_explicit_native_config(mini_repo):
    (mini_repo / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths=["src"]\npytest_add_cli_args=["--timeout=12"]\n'
    )
    (mini_repo / "tests/test_budget.py").write_text('def test_budget(pytestconfig):\n    assert pytestconfig._env_timeout == 0.5\n')
    result = run_native(mini_repo, SETUP + 'assert runner.execute_pytest(["tests", "-q"]) == 0\n',
                        DEADCODE_MUTMUT_TEST_TIMEOUT_SECONDS="0.5")
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_budget_does_not_inherit_broad_global_suite_addopts(mini_repo):
    (mini_repo / "pytest.ini").write_text('[pytest]\naddopts=--timeout=120 --timeout-method=thread\n')
    (mini_repo / "tests/test_budget.py").write_text(
        'def test_budget(pytestconfig):\n    assert pytestconfig._env_timeout == 5\n'
    )
    result = run_native(mini_repo, SETUP + 'assert runner.execute_pytest(["tests", "-q"]) == 0\n')
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_budget_and_ordinary_baseline(mini_repo):
    (mini_repo / "tests/test_budget.py").write_text(
        'def test_budget(pytestconfig):\n    assert pytestconfig._env_timeout == 5\n'
    )
    result = run_native(mini_repo, SETUP + 'assert runner.execute_pytest(["tests", "-q"]) == 0\n')
    assert result.returncode == 0, result.stdout + result.stderr
