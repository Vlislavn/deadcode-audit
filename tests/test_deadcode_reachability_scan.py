"""Tests for the on-demand whole-tree reachability scan (scripts/deadcode/reachability_scan.py).

The scan reuses the Tier 4 resolver + the shared classifier; these tests drive ``scan()`` over a
controlled in-memory corpus (monkeypatched seams) so no real files or git are touched, plus a real CLI
smoke over ``src/``. Symbol names are deliberately unlike the motivating health-analytics cluster — the
classification keys off the confidence CLASS, not any value — so the rules are proven to generalise.
"""

import json
from pathlib import Path

import pytest

from deadcode_audit import reachability_scan
from deadcode_audit.cli import main
from deadcode_audit.reachability import (
    _ADVISORY_DECORATED,
    _ADVISORY_ORPHANED,
    _ADVISORY_TEST_ONLY,
    _ADVISORY_TEST_SUPPORT,
    _ADVISORY_UNRESOLVED,
    PublicSymbolDefinition,
)


def _def(symbol: str, *, file: str, line: int, decorated: bool = False) -> PublicSymbolDefinition:
    return PublicSymbolDefinition(Path(file), symbol, line, decorated)


# --- the load-bearing whole-word occurrence tokenizer (git grep -w semantics) ---


def test_occurrence_index_matches_whole_words_only() -> None:
    corpus = {
        # line 2 has _reset_helper and resetting — NEITHER is the whole word 'reset'
        Path("src/a.py"): "def reset():\n    return _reset_helper() + resetting\n",
        Path("src/b.py"): "x = reset\n",  # a real bare-word use
    }
    index = reachability_scan._occurrence_index(corpus, {"reset"})
    locations = index["reset"]
    assert (Path("src/b.py"), 1) in locations  # bare-word use counted
    assert (Path("src/a.py"), 1) in locations  # the def line (its own occurrence)
    assert (
        Path("src/a.py"),
        2,
    ) not in locations  # _reset_helper / resetting are NOT 'reset'


# --- scan() classifies + ranks across every class (the generalization test) ---


def test_scan_classifies_and_ranks_across_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defs = [
        _def("mounted_route", file="src/h.py", line=60, decorated=True),  # decorated + orphaned
        _def("widget_unused", file="src/a.py", line=10),  # truly orphaned, undecorated
        _def("render", file="src/b.py", line=20),  # same-name collision -> occurs elsewhere
        _def("dynamic_knob", file="src/d.py", line=30),  # getattr-string use -> occurs elsewhere
        _def("refresh_for_testing", file="src/f.py", line=40),  # test-support by convention
        _def("staged_export", file="src/g.py", line=50),  # unwired, test-covered
    ]
    monkeypatch.setattr(reachability_scan, "build_candidates", lambda: defs)
    monkeypatch.setattr(reachability_scan, "_runtime_corpus_files", lambda: [])
    monkeypatch.setattr(reachability_scan, "resolved_reference_exists", lambda *_a, **_k: False)
    # Only render + dynamic_knob occur elsewhere in runtime (a collision / a dynamic string).
    monkeypatch.setattr(
        reachability_scan,
        "_occurrence_index",
        lambda _corpus, _names: {
            "render": {(Path("src/c.py"), 5)},
            "dynamic_knob": {(Path("src/e.py"), 8)},
        },
    )
    monkeypatch.setattr(
        reachability_scan,
        "_test_reference_names",
        lambda _names: {"refresh_for_testing", "staged_export"},
    )

    rows = reachability_scan.scan()
    reason_by_symbol = {definition.symbol: reason for definition, reason in rows}

    assert reason_by_symbol["widget_unused"] == _ADVISORY_ORPHANED
    assert reason_by_symbol["staged_export"] == _ADVISORY_TEST_ONLY
    # occurs-in-runtime symbols are UNRESOLVED, NOT orphaned — the grep-floor invariant holds whole-tree
    assert reason_by_symbol["render"] == _ADVISORY_UNRESOLVED
    assert reason_by_symbol["dynamic_knob"] == _ADVISORY_UNRESOLVED
    # decoration takes precedence over orphaned: a decorated symbol is framework-live, never orphaned
    assert reason_by_symbol["mounted_route"] == _ADVISORY_DECORATED
    assert reason_by_symbol["refresh_for_testing"] == _ADVISORY_TEST_SUPPORT
    # the orphaned candidate sorts FIRST (highest deadness confidence)
    assert rows[0][0].symbol == "widget_unused"


def test_scan_never_blocks_via_run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(reachability_scan, "build_candidates", lambda: [])
    monkeypatch.setattr(reachability_scan, "_runtime_corpus_files", lambda: [])
    monkeypatch.setattr(reachability_scan, "_test_reference_names", lambda _names: set())
    assert reachability_scan.run(top=0, as_json=False) == 0  # advisory: always exit 0
    assert "reachability scan: 0 src public symbol(s)" in capsys.readouterr().out


def test_top_truncates_only_noise_never_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defs = [
        _def("orphan_a", file="src/a.py", line=1),  # actionable (orphaned)
        _def("noise_b", file="src/b.py", line=2),
        _def("noise_c", file="src/c.py", line=3),
        _def("noise_d", file="src/d.py", line=4),
    ]
    monkeypatch.setattr(reachability_scan, "build_candidates", lambda: defs)
    monkeypatch.setattr(reachability_scan, "_runtime_corpus_files", lambda: [])
    monkeypatch.setattr(reachability_scan, "resolved_reference_exists", lambda *_a, **_k: False)
    # the three noise symbols occur elsewhere -> UNRESOLVED (lower-confidence, truncatable)
    monkeypatch.setattr(
        reachability_scan,
        "_occurrence_index",
        lambda _c, _n: {sym: {(Path("src/z.py"), 9)} for sym in ("noise_b", "noise_c", "noise_d")},
    )
    monkeypatch.setattr(reachability_scan, "_test_reference_names", lambda _n: set())

    rows = reachability_scan.scan()
    shown = reachability_scan._shown_rows(rows, top=1)
    symbols = [definition.symbol for definition, _reason in shown]
    assert "orphan_a" in symbols  # actionable class is never truncated...
    assert sum(s.startswith("noise_") for s in symbols) == 1  # ...only noise is capped at --top


# --- CLI smoke over the real src/ tree (no git, in-memory floor; always exit 0) ---


def test_cli_reachability_scan_json_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["reachability-scan", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "symbols" in payload and isinstance(payload["symbols"], list)
    assert payload["count"] == len(payload["symbols"])
