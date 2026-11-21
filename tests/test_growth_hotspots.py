"""``growth --hotspots``: commits x lines x max complexity (Python via ruff, JS via Oxlint) on a tiny real git history."""

import shutil
import subprocess

import pytest

from deadcode_audit import cli, diffscope, growth

BRANCHY = (
    "def f(x):\n    if x == 1:\n        return 1\n    elif x == 2:\n        return 2\n"
    "    elif x == 3:\n        return 3\n    return {n}\n"
)  # 8 lines, McCabe 4
JS_BRANCHY = (
    "export function f(x) {\n  if (x === 1) return 1;\n  else if (x === 2) return 2;\n"
    "  else if (x === 3) return 3;\n  return %d;\n}\n"
)  # 6 lines, complexity 4
FLAT = "".join(f"x{i} = {i}\n" for i in range(30))  # 30 lines, McCabe 1


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def commit(repo, **files):
    for name, text in files.items():
        path = repo / (name.replace("__", "/") + ".py")
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")


def commit_js(repo, **files):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")


def need_oxlint():
    if growth._oxlint() is None:
        pytest.skip("Oxlint 1.90.0 not available (set DEADCODE_OXLINT)")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.org")
    git(tmp_path, "config", "user.name", "T")
    commit(tmp_path, big="", hot=BRANCHY.format(n=0))
    commit(tmp_path, big=FLAT, tests__test_hot="def test_x():\n    pass\n")
    commit(tmp_path, hot=BRANCHY.format(n=1))
    commit(tmp_path, hot=BRANCHY.format(n=2))
    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    return tmp_path


def test_ranking_uses_ruff_complexity(repo):
    if shutil.which("ruff") is None:
        pytest.skip("ruff not on PATH")
    report = growth.hotspots(None)
    assert report["ruff"] is True
    assert [(r["path"], r["commits"], r["lines"], r["max_cc"], r["score"]) for r in report["files"]] == [
        ("hot.py", 3, 8, 4, 96),
        ("big.py", 2, 30, 1, 60),
    ]
    # The refactoring target is named: the most complex function and its length; a flat file has none.
    assert [(r["worst_function"], r["worst_lines"]) for r in report["files"]] == [("f", 8), (None, None)]
    assert growth.render_hotspots(report).splitlines()[2:] == ["       96       3      8      4  hot.py  (f, 8 lines)", "       60       2     30      1  big.py"]


def test_worst_function_is_the_most_complex_one(repo):
    if shutil.which("ruff") is None:
        pytest.skip("ruff not on PATH")
    commit(repo, hot="def easy(x):\n    if x:\n        return 1\n    return 0\n\n\n" + BRANCHY.format(n=3).replace("def f", "def hard"))
    row = growth.hotspots(None)["files"][0]
    assert (row["path"], row["max_cc"], row["worst_function"], row["worst_lines"]) == ("hot.py", 4, "hard", 8)


def test_without_ruff_complexity_is_omitted_and_said(repo, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert cli.main(["growth", "--hotspots"]) == 0
    out = capsys.readouterr().out
    assert "ruff not on PATH" in out and "max_cc" not in out and "worst function" not in out
    assert [line.split()[-1] for line in out.splitlines()[2:4]] == ["big.py", "hot.py"]  # 60 vs 24
    assert "Oxlint" not in out  # no JS file scored: no JS note


def test_range_top_and_base_requirement(repo, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    last = growth.hotspots("HEAD~2", top=1)  # the last two commits touch only hot.py
    assert [(r["path"], r["commits"]) for r in last["files"]] == [("hot.py", 2)]
    assert cli.main(["growth"]) == 2
    assert "--base is required unless --hotspots" in capsys.readouterr().err


def test_js_ranking_uses_oxlint_complexity(repo):
    need_oxlint()
    for n in range(3):
        commit_js(repo, **{"hot.js": JS_BRANCHY % n})
    commit_js(repo, **{"App.test.jsx": JS_BRANCHY % 0, "testSetup.js": JS_BRANCHY % 0, "__tests__/x.js": JS_BRANCHY % 0, "notes.ts": "export const a = 1;\n"})
    report = growth.hotspots(None)
    assert report["oxlint"] is True
    js = [r for r in report["files"] if r["path"].endswith((".js", ".jsx", ".ts"))]  # JS tests and .ts are not scored
    assert [(r["path"], r["commits"], r["lines"], r["max_cc"], r["score"], r["worst_function"], r["worst_lines"]) for r in js] == [("hot.js", 3, 6, 4, 72, "f", 6)]


def test_js_worst_function_is_named_or_anonymous(repo):
    need_oxlint()
    easy = "export function easy(x) {\n  if (x) return 1;\n  return 0;\n}\n\n"
    hard = "export const Hard = (x) => {\n  if (x === 1) return 1;\n  else if (x === 2) return 2;\n  else if (x === 3) return 3;\n  return 0;\n};\n"
    commit_js(repo, **{"c.jsx": easy + hard})
    row = next(r for r in growth.hotspots(None)["files"] if r["path"] == "c.jsx")
    assert (row["max_cc"], row["worst_function"], row["worst_lines"]) == (4, "anonymous@6:21", 6)


def test_mixed_python_and_js_rank_in_one_list(repo):
    if shutil.which("ruff") is None:
        pytest.skip("ruff not on PATH")
    need_oxlint()
    for n in range(3):
        commit_js(repo, **{"hot.js": JS_BRANCHY % n})
    report = growth.hotspots(None)
    assert [(r["path"], r["score"]) for r in report["files"]] == [("hot.py", 96), ("hot.js", 72), ("big.py", 60)]
    assert growth.render_hotspots(report).splitlines()[2:] == [
        "       96       3      8      4  hot.py  (f, 8 lines)",
        "       72       3      6      4  hot.js  (f, 6 lines)",
        "       60       2     30      1  big.py",
    ]


def test_without_oxlint_js_rows_stay_without_complexity_and_one_note(repo, monkeypatch, capsys):
    monkeypatch.setenv("DEADCODE_OXLINT", "/nonexistent/oxlint")
    for n in range(2):
        commit_js(repo, **{"hot.js": JS_BRANCHY % n, "also.jsx": JS_BRANCHY % n})
    report = growth.hotspots(None)
    assert report["oxlint"] is False
    js = [r for r in report["files"] if r["path"].endswith((".js", ".jsx"))]
    assert [(r["path"], r["score"]) for r in js] == [("also.jsx", 12), ("hot.js", 12)] and all("max_cc" not in r for r in js)  # commits x lines only
    assert cli.main(["growth", "--hotspots"]) == 0
    out = capsys.readouterr().out
    assert out.count("Oxlint 1.90.0 not available") == 1
    assert "hot.js" in out
