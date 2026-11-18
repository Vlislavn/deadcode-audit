"""stub-sensitivity: requirement scenarios, run end-to-end in a throwaway git repo."""
import hashlib
import json
import os
import subprocess
import sys

from deadcode_audit.stub_sensitivity import find_functions, stub_source

MODULE = '''\
def asserted():
    return 1


def untested():
    return 2


def helper():
    return 3


def via_helper():
    return helper() + 1


def logs():
    x = 1
    print(x)
'''
TESTS = '''\
from mod import asserted, via_helper


def test_asserted():
    assert asserted() == 1


def test_indirect():
    assert via_helper() == 4
'''


def make_repo(root, tests=TESTS):
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src/mod.py").write_text(MODULE)
    (root / "tests/test_mod.py").write_text(tests)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    (root / ".deadcode.yml").write_text("project:\n  source_roots: [src]\n  test_roots: [tests]\n")
    git = ["git", "-c", "user.email=a@b.c", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=root, check=True)


def stub_run(root):
    return subprocess.run(
        [sys.executable, "-m", "deadcode_audit", "stub-sensitivity", "--json", "src/mod.py"],
        cwd=root, env=dict(os.environ, DEADCODE_REPO_ROOT=str(root)), capture_output=True, text=True, timeout=120, check=False,
    )


def digest(root):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


def test_flags_only_untested_function_and_leaves_tree_untouched(tmp_path):
    make_repo(tmp_path)
    before = digest(tmp_path)
    done = stub_run(tmp_path)
    assert done.returncode == 0, done.stderr
    by_name = {r["function"]: r for r in json.loads(done.stdout)["results"]}
    undetected = {n for n, r in by_name.items() if not r["detected"]}
    # `logs` is also untested; `helper` is only reached through via_helper and must be detected.
    assert undetected == {"untested", "logs"}
    assert by_name["asserted"]["detected"] and by_name["helper"]["detected"]
    assert by_name["logs"]["stub_kind"] == "raise_runtime_error"
    assert digest(tmp_path) == before
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert status.stdout == ""


def test_red_baseline_exits_nonzero_with_message(tmp_path):
    make_repo(tmp_path, TESTS + "\n\ndef test_red():\n    assert False\n")
    done = stub_run(tmp_path)
    assert done.returncode != 0
    assert "baseline red" in done.stderr
    assert "results" not in done.stdout


def test_stub_kinds_and_skips():
    spans, skipped = find_functions(
        "def a():\n    return 1\n\ndef b():\n    print(1)\n\ndef c():\n    pass\n\ndef d():\n    yield 1\n"
    )
    assert [(s.locator, s.stub_kind) for s in spans] == [("a", "return_none"), ("b", "raise_runtime_error")]
    assert {s["function"]: s["reason"] for s in skipped} == {"c": "empty body", "d": "generator"}
    assert stub_source("def a():\n    return 1\n", spans[0]) == "def a():\n    return None\n"
