"""Tests for the import-cycle guard (scripts/deadcode/cycles.py).

The Tarjan SCC core is exercised on synthetic graphs with names unrelated to any real module, so the
algorithm is proven generally (not fitted to the source monorepo's current 10 cycles). ``run()`` is driven over a
controlled graph to pin the advisory-vs-blocking + allowlist (ratchet) contract, plus a real CLI smoke.
"""

import json
from pathlib import Path

import pytest

from deadcode_audit import config, cycles
from fixtures.deadcode_cli import run_deadcode_cli

# --- Tarjan SCC / find_cycles ---


def test_find_cycles_detects_2_and_3_cycles_ignores_dag() -> None:
    graph = {
        "a": {"b"},
        "b": {"a"},  # 2-cycle a<->b
        "c": {"d"},
        "d": {"e"},
        "e": {"c"},  # 3-cycle c->d->e->c
        "f": {"g"},
        "g": set(),  # acyclic
        "h": set(),  # isolated
    }
    found = cycles.find_cycles(graph)
    assert ["a", "b"] in found
    assert ["c", "d", "e"] in found
    assert len(found) == 2  # no singleton/DAG node is ever a cycle
    assert all(node not in cycle for cycle in found for node in ("f", "g", "h"))


def test_find_cycles_self_loop_is_not_a_cycle() -> None:
    # an SCC of size 1 (even with a self-edge) is not a cycle; build_import_graph also discards self-imports
    assert cycles.find_cycles({"a": {"a"}, "b": set()}) == []


def test_find_cycles_is_deterministic() -> None:
    graph = {"y": {"x"}, "x": {"y"}}
    assert cycles.find_cycles(graph) == [["x", "y"]]  # members sorted, list sorted


# --- edge resolution ---


def test_edge_target_prefers_internal_submodule_over_package() -> None:
    nodes = {"pkg.core": Path("a"), "pkg.core.config": Path("b")}
    # `from pkg.core import config` depends on the submodule when it is itself internal
    assert cycles._edge_target("pkg.core", "config", nodes) == "pkg.core.config"
    # `from pkg.core import Settings` (a symbol, not a module) depends on the package
    assert cycles._edge_target("pkg.core", "Settings", nodes) == "pkg.core"
    # an external module resolves to nothing (no edge)
    assert cycles._edge_target("os.path", "join", nodes) is None


# --- run(): advisory vs blocking + allowlist ratchet ---


def _graph_with_one_cycle(**_: bool) -> dict[str, set[str]]:
    return {"x": {"y"}, "y": {"x"}, "z": set()}


def test_run_blocking_fails_advisory_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cycles, "build_import_graph", _graph_with_one_cycle)
    monkeypatch.setattr(cycles.config, "load_deadcode_config", lambda _root: config.DeadcodeConfig())
    assert cycles.run(as_json=False, advisory=False) == 1  # a non-allowlisted cycle blocks
    assert cycles.run(as_json=False, advisory=True) == 0  # advisory never blocks


def test_run_allowlist_ratchet_unblocks_known_cycle(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cycles, "build_import_graph", _graph_with_one_cycle)
    key = cycles._cycle_key(["x", "y"])
    monkeypatch.setattr(
        cycles.config,
        "load_deadcode_config",
        lambda _root: config.DeadcodeConfig(allowed_cycles=(key,)),
    )
    # baselined cycle no longer blocks (ratchet) ...
    assert cycles.run(as_json=False, advisory=False) == 0
    # ... but the advisory audit still SHOWS it, tagged as allowlisted (honest report)
    assert cycles.run(as_json=False, advisory=True) == 0
    assert "[allowlisted]" in capsys.readouterr().out


def test_run_new_cycle_still_blocks_despite_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # allowlist x<->y, but a SECOND, un-listed cycle p<->q must still fail the blocking gate
    monkeypatch.setattr(
        cycles,
        "build_import_graph",
        lambda **_: {"x": {"y"}, "y": {"x"}, "p": {"q"}, "q": {"p"}},
    )
    monkeypatch.setattr(
        cycles.config,
        "load_deadcode_config",
        lambda _root: config.DeadcodeConfig(allowed_cycles=(cycles._cycle_key(["x", "y"]),)),
    )
    assert cycles.run(as_json=False, advisory=False) == 1


# --- CLI smoke over the real src/ tree ---


def test_cli_cycles_advisory_json_exits_zero(tmp_path: Path) -> None:
    source = tmp_path / "src" / "modules"
    source.mkdir(parents=True)
    (source / "left.py").write_text("from modules import right\n", encoding="utf-8")
    (source / "right.py").write_text("from modules import left\n", encoding="utf-8")
    result = run_deadcode_cli(tmp_path, "cycles", "--advisory", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "advisory": True,
        "count": 1,
        "cycles": [
            {
                "key": "modules.left <-> modules.right",
                "members": ["modules.left", "modules.right"],
                "kind": "hard",
                "allowed": False,
            }
        ],
    }


# --- TYPE_CHECKING / lazy / hard classification (real CLI over a tmp corpus) ---


def _corpus(tmp_path: Path, left: str, right: str) -> Path:
    source = tmp_path / "src" / "modules"
    source.mkdir(parents=True)
    (source / "left.py").write_text(left, encoding="utf-8")
    (source / "right.py").write_text(right, encoding="utf-8")
    return tmp_path


def _kinds(root: Path, *flags: str) -> tuple[int, list[str]]:
    result = run_deadcode_cli(root, "cycles", "--json", *flags)
    return result.returncode, [c["kind"] for c in json.loads(result.stdout)["cycles"]]


_TOP = "from modules import right\n"
_BACK = "from modules import left\n"
_LAZY = "def f():\n    from modules import left\n    return left\n"


@pytest.mark.parametrize(
    "guard",
    [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from modules import left\n",
        "import typing\nif typing.TYPE_CHECKING:\n    from modules import left\n",
    ],
)
def test_type_checking_import_is_not_an_edge(tmp_path: Path, guard: str) -> None:
    root = _corpus(tmp_path, _TOP, guard)
    assert _kinds(root, "--advisory") == (0, [])
    assert _kinds(root, "--advisory", "--strict-lazy") == (0, [])
    assert _kinds(root) == (0, [])


def test_type_checking_else_branch_is_runtime(tmp_path: Path) -> None:
    guard = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    from modules import left\n"
    assert _kinds(_corpus(tmp_path, _TOP, guard)) == (1, ["hard"])


def test_cycle_through_one_lazy_import_is_soft_and_does_not_block(tmp_path: Path) -> None:
    root = _corpus(tmp_path, _TOP, _LAZY)
    assert _kinds(root) == (0, ["soft"])
    assert _kinds(root, "--advisory") == (0, ["soft"])


def test_two_top_level_imports_are_hard_and_block(tmp_path: Path) -> None:
    root = _corpus(tmp_path, _TOP, _BACK)
    assert _kinds(root) == (1, ["hard"])


def test_strict_lazy_restores_lazy_edges_as_hard(tmp_path: Path) -> None:
    root = _corpus(tmp_path, _TOP, _LAZY)
    assert _kinds(root, "--strict-lazy") == (1, ["hard"])


def test_text_output_lists_hard_first_then_soft_section(tmp_path: Path) -> None:
    source = tmp_path / "src" / "modules"
    source.mkdir(parents=True)
    (source / "a.py").write_text("from modules import b\n", encoding="utf-8")
    (source / "b.py").write_text("from modules import a\n", encoding="utf-8")
    (source / "c.py").write_text("from modules import d\n", encoding="utf-8")
    (source / "d.py").write_text("def f():\n    from modules import c\n", encoding="utf-8")
    result = run_deadcode_cli(tmp_path, "cycles")
    assert result.returncode == 1
    out = result.stdout
    assert "1 circular import group(s)" in out
    assert out.index("modules.a -> modules.b") < out.index("soft cycle(s)") < out.index("modules.c -> modules.d")
