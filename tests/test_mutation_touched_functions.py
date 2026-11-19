"""Touched-function mutation scoping: which lines mutmut may mutate for a diff."""

import subprocess
from pathlib import Path

import pytest
from mutmut.mutation.file_mutation import mutate_file_contents

from deadcode_audit import cli, diffscope, mutation

THREE = (
    "def a(x):\n    return x + 1\n\n\n"
    "def b(x):\n    return x * 2\n\n\n"
    "def c(x):\n    return x - 3\n"
)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.org")
    git(tmp_path, "config", "user.name", "T")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "logic.py").write_text(THREE)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "checkout", "-b", "change")
    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    return tmp_path


def commit(repo, text, name="logic.py"):
    (repo / "runtime" / name).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")


def touched(repo, name="logic.py"):
    return mutation.build_touched_lines([Path("runtime") / name], "main")


def mutated_functions(source, lines):
    names = mutate_file_contents("x.py", source, set(lines) if lines is not None else None).mutant_names
    return {n.split("__mutmut_")[0] for n in names}


def test_diff_touching_one_of_three_functions_mutates_only_it(repo):
    commit(repo, THREE.replace("x * 2", "x * 5"))
    lines = touched(repo)["runtime/logic.py"]
    assert lines == {5, 6}
    assert mutated_functions(THREE.replace("x * 2", "x * 5"), lines) == {"x_b"}
    assert mutated_functions(THREE, None) == {"x_a", "x_b", "x_c"}


def test_deleted_line_inside_function_touches_it(repo):
    with_extra = THREE.replace("    return x * 2\n", "    y = x\n    return y * 2\n")
    commit(repo, with_extra)
    git(repo, "checkout", "-b", "deleter")
    commit(repo, THREE)  # only deletes a line of b
    lines = mutation.build_touched_lines([Path("runtime/logic.py")], "change")["runtime/logic.py"]
    assert lines == {5, 6}


def test_decorator_or_signature_only_change_touches_function(repo):
    commit(repo, THREE.replace("def c(x):", "def c(x, y=1):"))
    assert touched(repo)["runtime/logic.py"] == {9, 10}
    git(repo, "checkout", "main", "--", ".")
    commit(repo, "import functools\n\n" + THREE.replace("def a(x):", "@functools.cache\ndef a(x):"))
    assert 3 in touched(repo)["runtime/logic.py"]


def test_module_level_only_change_selects_nothing_and_says_so(repo, capsys, monkeypatch):
    commit(repo, "LIMIT = 3\n" + THREE)
    assert touched(repo)["runtime/logic.py"] == frozenset()
    assert "runtime/logic.py: changed lines outside functions not mutated" in capsys.readouterr().err
    called = []
    monkeypatch.setattr(mutation, "_reset_mutants_dir", lambda: called.append(1))
    code = mutation.run_mutmut_for_paths([Path("runtime/logic.py")], touched(repo))
    assert code == 0
    assert "No mutants: no touched functions" in capsys.readouterr().out
    assert called == []  # mutmut was never started


def test_added_file_is_unrestricted(repo):
    commit(repo, THREE, name="new.py")
    assert "runtime/new.py" not in touched(repo, "new.py")


def test_dirty_working_tree_is_unrestricted(repo, capsys):
    commit(repo, THREE.replace("x * 2", "x * 5"))
    (repo / "runtime" / "logic.py").write_text(THREE.replace("x * 2", "x * 5") + "\n# dirty\n")
    assert touched(repo) == {}
    assert "working tree differs from HEAD" in capsys.readouterr().err


def test_flag_defaults_off(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.diffscope, "changed_python_files", lambda _b: [Path("runtime/logic.py")])
    monkeypatch.setattr(cli.mutation, "build_mutation_targets", lambda paths, compare_branch=None: list(paths))
    monkeypatch.setattr(cli.mutation, "run_mutmut_for_paths", lambda paths, touched=None: seen.update(touched=touched) or 0)
    monkeypatch.setattr(cli.mutation, "build_touched_lines", lambda *_: {"runtime/logic.py": frozenset({1})})
    parser = cli._build_parser()
    cli._cmd_run_mutmut_changed(parser.parse_args(["run-mutmut-changed"]))
    assert seen == {"touched": None}
    cli._cmd_run_mutmut_changed(parser.parse_args(["run-mutmut-changed", "--only-touched-functions"]))
    assert seen == {"touched": {"runtime/logic.py": frozenset({1})}}
