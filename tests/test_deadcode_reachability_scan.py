"""Tests for the on-demand whole-tree reachability scan (scripts/deadcode/reachability_scan.py).

The scan reuses the Tier 4 resolver + the shared classifier; these tests drive ``scan()`` over a
controlled in-memory corpus (monkeypatched seams) so no real files or git are touched, plus a real CLI
smoke over ``src/``. Symbol names are deliberately unlike the motivating health-analytics cluster — the
classification keys off the confidence CLASS, not any value — so the rules are proven to generalise.
"""

import json
from pathlib import Path

import pytest

from deadcode_audit.structure import reachability_scan
from fixtures.deadcode_cli import run_deadcode_cli
from deadcode_audit.structure.reachability import (
    _ADVISORY_DECORATED,
    _ADVISORY_ORPHANED,
    ADVISORY_TEST_ONLY,
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
    monkeypatch.setattr(reachability_scan, "runtime_corpus_files", lambda: [])
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
    assert reason_by_symbol["staged_export"] == ADVISORY_TEST_ONLY
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
    monkeypatch.setattr(reachability_scan, "runtime_corpus_files", lambda: [])
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
    monkeypatch.setattr(reachability_scan, "runtime_corpus_files", lambda: [])
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


def test_cli_reachability_scan_json_exits_zero(tmp_path: Path) -> None:
    source = tmp_path / "src" / "modules"
    source.mkdir(parents=True)
    (source / "library.py").write_text(
        "def consumed():\n    return 1\n\ndef orphaned():\n    return 2\n", encoding="utf-8"
    )
    (source / "consumer.py").write_text("from modules.library import consumed\nvalue = consumed()\n", encoding="utf-8")
    result = run_deadcode_cli(tmp_path, "reachability-scan", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "count": 1,
        "symbols": [
            {
                "file": "src/modules/library.py",
                "line": 4,
                "symbol": "orphaned",
                "decorated": False,
                "reason": _ADVISORY_ORPHANED,
            }
        ],
    }


# --- the configured Vulture whitelist declares names consumed (real CLI over a tmp corpus) ---


def _whitelist_project(tmp_path: Path, *, whitelist_text: str | None) -> None:
    """Two dead functions in ``pkg``; the whitelist (if any) names only ``documented_tool`` (+ a comment word)."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "tools.py").write_text(
        "def documented_tool():\n    return 1\n\ndef documented_tool_two():\n    return 2\n", encoding="utf-8"
    )
    config = "project:\n  source_roots: [pkg]\n"
    if whitelist_text is not None:
        (tmp_path / "whitelist.py").write_text(whitelist_text, encoding="utf-8")
        config += "  vulture_whitelist: [whitelist.py]\n"
    (tmp_path / ".deadcode.yml").write_text(config, encoding="utf-8")


def _scan_symbols(tmp_path: Path) -> list[str]:
    result = run_deadcode_cli(tmp_path, "reachability-scan", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    return sorted(row["symbol"] for row in json.loads(result.stdout)["symbols"])


def test_scan_without_whitelist_reports_both_dead_symbols(tmp_path: Path) -> None:
    _whitelist_project(tmp_path, whitelist_text=None)
    assert _scan_symbols(tmp_path) == ["documented_tool", "documented_tool_two"]


def test_scan_honors_configured_whitelist_but_still_reports_similar_dead_name(tmp_path: Path) -> None:
    # bare-name form + a comment mentioning the other name: only the real Name node counts
    _whitelist_project(tmp_path, whitelist_text="documented_tool  # not documented_tool_two\n_.attr_form\n")
    assert _scan_symbols(tmp_path) == ["documented_tool_two"]


def test_vulture_whitelist_names_reads_names_and_attributes_skips_missing_and_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deadcode_audit import layout
    from deadcode_audit.structure import resolver

    (tmp_path / "ok.py").write_text("alpha\n_.beta\n# gamma\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    (tmp_path / ".deadcode.yml").write_text(
        "project:\n  vulture_whitelist: [ok.py, broken.py, missing.py]\n", encoding="utf-8"
    )
    monkeypatch.setattr(layout, "REPO_ROOT", tmp_path)
    assert resolver.vulture_whitelist_names() >= {"alpha", "beta"}
    assert "gamma" not in resolver.vulture_whitelist_names()


# --- reachability/duplicate-constant: one business string defined in 2+ runtime modules ---


def _duplicate_constant_sites(root: Path, files: dict[str, str]) -> dict[tuple[str, int], str]:
    (root / ".deadcode.yml").write_text("project:\n  source_roots: [src]\n  test_roots: [tests]\n  import_roots: [src, '.']\n")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    result = run_deadcode_cli(root, "scan", "--json")
    return {
        (d["filePath"], d["line"]): d["message"]
        for d in json.loads(result.stdout)["diagnostics"]
        if d["rule"] == "reachability/duplicate-constant"
    }


def test_duplicate_constant_flags_the_importing_side_and_names_the_owner(tmp_path: Path) -> None:
    shared = "triage-approved-token"
    flagged = _duplicate_constant_sites(
        tmp_path,
        {
            "src/app/__init__.py": "",
            "src/app/core.py": f'RULE = "{shared}"\nTWICE = "same-module-twice"\nTWICE_AGAIN = "same-module-twice"\n'
            'ENC = "utf-8"\nMAIN = "__main__"\nSHORT = "short-9ch"\nPATH = "os.path.join.module"\n'
            'EXPORTED = "exported-shared-value"\n__all__ = ["EXPORTED"]\n',
            "src/app/cli.py": f'from app.core import RULE\n\nLOCAL_RULE = "{shared}"\nENC = "utf-8"\nMAIN = "__main__"\n'
            'SHORT = "short-9ch"\nPATH = "os.path.join.module"\nEXPORTED = "exported-shared-value"\n'
            '__all__ = ["EXPORTED"]\n\n\ndef main():\n    return RULE, LOCAL_RULE\n',
            "tests/test_app.py": f'TEST_RULE = "{shared}"\n',
        },
    )
    assert list(flagged) == [("src/app/cli.py", 3)]  # not core.py (the owner), tests, short/common/dotted/__all__/same-module
    assert "src/app/core.py:1" in flagged[("src/app/cli.py", 3)]
    assert "define it once in src/app/core.py" in flagged[("src/app/cli.py", 3)]


def test_duplicate_constant_without_import_relation_lists_locations_only(tmp_path: Path) -> None:
    text = 'LIMIT_TEXT = "the same rule text"\n'
    flagged = _duplicate_constant_sites(tmp_path, {"src/app/a.py": text, "src/app/b.py": text})
    assert sorted(flagged) == [("src/app/a.py", 1), ("src/app/b.py", 1)]
    assert all("define it once" not in message for message in flagged.values())
