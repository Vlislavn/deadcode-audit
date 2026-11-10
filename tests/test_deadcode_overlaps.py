"""Tests for the on-demand function-overlap audit (scripts/deadcode/overlaps.py).

A FAKE embedder is injected so the real code-embedding model never loads (no torch, no network).
Covers: the combination math, descending ranking, multi-overlap (a function surfaces in >=2 pairs —
proves NOT best-match-only), symmetric dedup, the embedding signal changing inclusion, the
deterministic --no-embed path, the min-tokens prefilter, source extraction (+ None fallback), the
backend None-degrade contract, and a real CLI smoke run.
"""

import ast
import json
from pathlib import Path

import numpy as np
import pytest

from deadcode_audit import clones, overlap_embed, overlaps
from deadcode_audit.cli import main
from fixtures.deadcode_cli import run_deadcode_cli


def _func(
    symbol: str, *, tokens: tuple[str, ...], semantic: frozenset[str], source: str = "", line: int = 1
) -> overlaps._SourcedFunc:
    record = clones.FuncRecord(Path("src/x.py"), line, symbol, tokens, semantic)
    return overlaps._SourcedFunc(record=record, source=source)


# A fake embedder: maps a source marker to a 2-D unit vector so cosine is fully deterministic.
# "AAA" and "CCC" are identical (cosine 1.0); "BBB" is orthogonal (cosine 0.0).
_FAKE_VECTORS = {"AAA": [1.0, 0.0], "BBB": [0.0, 1.0], "CCC": [1.0, 0.0]}


def _fake_embed(sources: list[str]) -> np.ndarray:
    return np.array([_FAKE_VECTORS.get(s.strip(), [1.0, 0.0]) for s in sources], dtype=float)


# --- combination math ---


def test_combination_weights() -> None:
    assert overlaps._combined(0.8, 0.5) == pytest.approx(0.6 * 0.8 + 0.4 * 0.5)


# --- ranking + multi-overlap + symmetric dedup (deterministic mode) ---

_TOK = ("Return", "V0", "#helper")  # identical tokens => struct 1.0
_SEM = frozenset({"#helper"})  # identical semantic => api 1.0


def test_descending_rank_and_multi_overlap_and_dedup() -> None:
    funcs = [
        _func("a", tokens=_TOK, semantic=_SEM, line=1),
        _func("b", tokens=_TOK, semantic=_SEM, line=2),
        _func("c", tokens=_TOK, semantic=_SEM, line=3),
    ]
    pairs = overlaps.find_overlaps(funcs, threshold=0.5, min_tokens=1)
    # symmetric dedup: exactly C(3,2)=3 unique pairs, no mirror duplicates
    assert len(pairs) == 3
    keys = {frozenset({(p.a_line, p.a_symbol), (p.b_line, p.b_symbol)}) for p in pairs}
    assert len(keys) == 3
    # descending by combined
    assert [p.combined for p in pairs] == sorted((p.combined for p in pairs), reverse=True)
    # multi-overlap: symbol 'a' appears in >=2 pairs (NOT best-match-only)
    a_pairs = [p for p in pairs if "a" in (p.a_symbol, p.b_symbol)]
    assert len(a_pairs) == 2


# --- pass 2: embed-only candidates not already in pass 1 ---


def test_embed_candidates_add_only_pairs_pass1_missed() -> None:
    # a~c have identical embeddings but disjoint API names, so the api gate drops them in pass 1.
    other = frozenset({"#other"})
    funcs = [
        _func("a", tokens=_TOK, semantic=_SEM, source="AAA", line=1),
        _func("b", tokens=_TOK, semantic=_SEM, source="BBB", line=2),
        _func("c", tokens=_TOK, semantic=other, source="CCC", line=3),
    ]
    pass1 = overlaps.find_overlaps(funcs, threshold=0.55, min_tokens=1)
    assert {frozenset({p.a_symbol, p.b_symbol}) for p in pass1} == {frozenset({"a", "b"})}
    extra = overlaps.find_embed_candidates(funcs, min_tokens=1, embed_fn=_fake_embed, known=pass1, topk=1)
    assert [(p.a_symbol, p.b_symbol) for p in extra] == [("a", "c")]  # a~b is already reported in pass 1
    assert extra[0].embed == pytest.approx(1.0)
    assert all(p.embed is None for p in pass1)


def test_threshold_boundary() -> None:
    funcs = [_func("a", tokens=_TOK, semantic=_SEM, line=1), _func("b", tokens=_TOK, semantic=_SEM, line=2)]
    combined = overlaps._combined(1.0, 1.0)  # struct=api=1 -> 1.0
    assert len(overlaps.find_overlaps(funcs, threshold=combined, min_tokens=1)) == 1
    assert len(overlaps.find_overlaps(funcs, threshold=combined + 0.01, min_tokens=1)) == 0


def test_min_tokens_prefilter() -> None:
    short = _func("short", tokens=("Return",), semantic=_SEM, line=1)  # 1 token
    full = _func("full", tokens=_TOK, semantic=_SEM, line=2)
    pairs = overlaps.find_overlaps([short, full], threshold=0.5, min_tokens=40)
    assert pairs == []  # both filtered (neither >= 40 tokens) -> no pairs


# --- rendering ---


def test_render_human_shows_embed_na_when_pass1_only() -> None:
    funcs = [_func("a", tokens=_TOK, semantic=_SEM, line=1), _func("b", tokens=_TOK, semantic=_SEM, line=2)]
    pairs = overlaps.find_overlaps(funcs, threshold=0.5, min_tokens=1)
    text = overlaps._render_human(pairs, [], embed_active=False, threshold=0.5, model="m")
    assert "pass 1 only, embed n/a" in text
    assert "embed n/a" in text


# --- source extraction (build_corpus) ---


def test_build_corpus_captures_source(monkeypatch: pytest.MonkeyPatch) -> None:
    module_src = "def alpha():\n    return 1\n\n\ndef beta(x):\n    return x + 1\n"
    monkeypatch.setattr(overlaps.diffscope, "all_src_files", lambda: [Path("src/m.py")])
    monkeypatch.setattr(overlaps.diffscope, "read_text", lambda _p: module_src)
    funcs = overlaps.build_corpus()
    by_symbol = {f.record.symbol: f for f in funcs}
    assert by_symbol["alpha"].source == ast.get_source_segment(module_src, ast.parse(module_src).body[0])
    assert by_symbol["beta"].source.startswith("def beta(x):")


def test_build_corpus_none_segment_falls_back_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    module_src = "def alpha():\n    return 1\n"
    monkeypatch.setattr(overlaps.diffscope, "all_src_files", lambda: [Path("src/m.py")])
    monkeypatch.setattr(overlaps.diffscope, "read_text", lambda _p: module_src)
    monkeypatch.setattr(overlaps.ast, "get_source_segment", lambda *_a, **_k: None)  # force the fail-open path
    funcs = overlaps.build_corpus()
    assert funcs and all(f.source == "" for f in funcs)  # None -> "" , never a crash


# --- backend degrade contract ---


def test_requested_embedding_failure_is_not_silently_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("embedding dependency missing")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ImportError, match="embedding dependency missing"):
        overlap_embed.get_embedder("requested-model")


def test_cli_overlaps_no_embed_json_exits_zero(tmp_path: Path) -> None:
    source = tmp_path / "src" / "numbers.py"
    source.parent.mkdir()
    source.write_text(
        "\n".join(f"def {name}(values):\n    return sum(values)\n" for name in ("alpha", "beta", "gamma", "delta")),
        encoding="utf-8",
    )
    result = run_deadcode_cli(tmp_path, "overlaps", "--no-embed", "--json", "--min-tokens", "1", "--top", "3")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["embed_active"] is False
    assert payload["model"] is None
    assert payload["count"] == 3
    assert [(pair["a"]["symbol"], pair["b"]["symbol"]) for pair in payload["pairs"]] == [
        ("alpha", "beta"),
        ("alpha", "gamma"),
        ("alpha", "delta"),
    ]
    assert all(
        pair["a"]["file"] == pair["b"]["file"] == "src/numbers.py"
        and pair["combined"] == pair["struct"] == pair["api"] == 1.0
        and pair["embed"] is None
        for pair in payload["pairs"]
    )


# --- two-pass CLI default, fallback, --no-embed ---

_CLI_FUNCS = [
    _func("a", tokens=_TOK, semantic=_SEM, source="AAA", line=1),
    _func("b", tokens=_TOK, semantic=frozenset({"#other"}), source="CCC", line=2),
]


def _patch_corpus(monkeypatch: pytest.MonkeyPatch, embedder) -> list[str]:
    calls: list[str] = []

    def get_embedder(model: str):
        calls.append(model)
        return embedder(model)

    monkeypatch.setattr(overlaps, "build_corpus", lambda: _CLI_FUNCS)
    monkeypatch.setattr(overlap_embed, "get_embedder", get_embedder)
    monkeypatch.setattr(overlap_embed, "model_metadata", lambda: {"model": "fake"})
    return calls


def test_cli_default_runs_pass2_with_pinned_model(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = _patch_corpus(monkeypatch, lambda _m: _fake_embed)
    assert main(["overlaps", "--json", "--min-tokens", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == ["codesage/codesage-small-v2"]
    assert payload["embed_active"] is True
    assert payload["pairs"] == []  # api gate drops a~b in pass 1
    assert [(p["a"]["symbol"], p["b"]["symbol"]) for p in payload["embed_pairs"]] == [("a", "b")]


def test_cli_falls_back_to_pass1_with_one_warning_when_model_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_model: str):
        raise ImportError("No module named 'torch'")

    _patch_corpus(monkeypatch, missing)
    assert main(["overlaps", "--json", "--min-tokens", "1"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["embed_active"] is False and payload["embed_pairs"] == []
    warnings = [line for line in captured.err.splitlines() if line.strip()]
    assert len(warnings) == 1 and warnings[0].startswith("warning:") and "torch" in warnings[0]


def test_cli_no_embed_never_loads_model(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = _patch_corpus(monkeypatch, lambda _m: _fake_embed)
    assert main(["overlaps", "--no-embed", "--json", "--min-tokens", "1"]) == 0
    captured = capsys.readouterr()
    assert calls == [] and captured.err == ""
    assert json.loads(captured.out)["embed_active"] is False
