"""Tests for the import-cycle guard (scripts/deadcode/cycles.py).

The Tarjan SCC core is exercised on synthetic graphs with names unrelated to any real module, so the
algorithm is proven generally (not fitted to the source monorepo's current 10 cycles). ``run()`` is driven over a
controlled graph to pin the advisory-vs-blocking + allowlist (ratchet) contract, plus a real CLI smoke.
"""

import json
from pathlib import Path

import pytest

from deadcode_audit import config, cycles
from deadcode_audit.cli import main

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


def _graph_with_one_cycle() -> dict[str, set[str]]:
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
        lambda: {"x": {"y"}, "y": {"x"}, "p": {"q"}, "q": {"p"}},
    )
    monkeypatch.setattr(
        cycles.config,
        "load_deadcode_config",
        lambda _root: config.DeadcodeConfig(allowed_cycles=(cycles._cycle_key(["x", "y"]),)),
    )
    assert cycles.run(as_json=False, advisory=False) == 1


# --- CLI smoke over the real src/ tree ---


def test_cli_cycles_advisory_json_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["cycles", "--advisory", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["advisory"] is True
    assert "cycles" in payload and isinstance(payload["cycles"], list)
