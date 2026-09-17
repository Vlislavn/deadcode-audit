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
    assert overlaps._combined_with_embed(0.8, 0.5, 0.9) == pytest.approx(0.5 * 0.9 + 0.3 * 0.8 + 0.2 * 0.5)
    assert overlaps._combined_deterministic(0.8, 0.5) == pytest.approx(0.6 * 0.8 + 0.4 * 0.5)


# --- ranking + multi-overlap + symmetric dedup (deterministic mode) ---

_TOK = ("Return", "V0", "#helper")  # identical tokens => struct 1.0
_SEM = frozenset({"#helper"})  # identical semantic => api 1.0


def test_descending_rank_and_multi_overlap_and_dedup() -> None:
    funcs = [
        _func("a", tokens=_TOK, semantic=_SEM, line=1),
        _func("b", tokens=_TOK, semantic=_SEM, line=2),
        _func("c", tokens=_TOK, semantic=_SEM, line=3),
    ]
    pairs = overlaps.find_overlaps(funcs, threshold=0.5, min_tokens=1, embed_fn=None)
    # symmetric dedup: exactly C(3,2)=3 unique pairs, no mirror duplicates
    assert len(pairs) == 3
    keys = {frozenset({(p.a_line, p.a_symbol), (p.b_line, p.b_symbol)}) for p in pairs}
    assert len(keys) == 3
    # descending by combined
    assert [p.combined for p in pairs] == sorted((p.combined for p in pairs), reverse=True)
    # multi-overlap: symbol 'a' appears in >=2 pairs (NOT best-match-only)
    a_pairs = [p for p in pairs if "a" in (p.a_symbol, p.b_symbol)]
    assert len(a_pairs) == 2


# --- embedding signal changes inclusion (hybrid vs deterministic) ---


def test_embedding_signal_filters_pairs() -> None:
    funcs = [
        _func("a", tokens=_TOK, semantic=_SEM, source="AAA", line=1),
        _func("b", tokens=_TOK, semantic=_SEM, source="BBB", line=2),  # orthogonal embed
        _func("c", tokens=_TOK, semantic=_SEM, source="CCC", line=3),  # == a
    ]
    # Hybrid: struct=api=1 for all; only a~c has embed 1.0 -> combined 1.0; a~b,b~c embed 0 -> 0.5 < 0.55.
    hybrid = overlaps.find_overlaps(funcs, threshold=0.55, min_tokens=1, embed_fn=_fake_embed)
    assert len(hybrid) == 1
    only = hybrid[0]
    assert {only.a_symbol, only.b_symbol} == {"a", "c"}
    assert only.embed == pytest.approx(1.0)
    # Deterministic: embed ignored, all three pairs pass (0.6+0.4 = 1.0), every embed is None.
    deterministic = overlaps.find_overlaps(funcs, threshold=0.55, min_tokens=1, embed_fn=None)
    assert len(deterministic) == 3
    assert all(p.embed is None for p in deterministic)


def test_threshold_boundary() -> None:
    funcs = [_func("a", tokens=_TOK, semantic=_SEM, line=1), _func("b", tokens=_TOK, semantic=_SEM, line=2)]
    combined = overlaps._combined_deterministic(1.0, 1.0)  # struct=api=1 -> 1.0
    assert len(overlaps.find_overlaps(funcs, threshold=combined, min_tokens=1, embed_fn=None)) == 1
    assert len(overlaps.find_overlaps(funcs, threshold=combined + 0.01, min_tokens=1, embed_fn=None)) == 0


def test_min_tokens_prefilter() -> None:
    short = _func("short", tokens=("Return",), semantic=_SEM, line=1)  # 1 token
    full = _func("full", tokens=_TOK, semantic=_SEM, line=2)
    pairs = overlaps.find_overlaps([short, full], threshold=0.5, min_tokens=40, embed_fn=None)
    assert pairs == []  # both filtered (neither >= 40 tokens) -> no pairs


# --- rendering ---


def test_render_human_shows_embed_na_when_deterministic() -> None:
    funcs = [_func("a", tokens=_TOK, semantic=_SEM, line=1), _func("b", tokens=_TOK, semantic=_SEM, line=2)]
    pairs = overlaps.find_overlaps(funcs, threshold=0.5, min_tokens=1, embed_fn=None)
    text = overlaps._render_human(pairs, embed_active=False, threshold=0.5, model="m")
    assert "deterministic-only, embed n/a" in text
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


def test_cli_overlaps_no_embed_json_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["overlaps", "--no-embed", "--json", "--top", "3"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["embed_active"] is False
    assert "pairs" in payload and isinstance(payload["pairs"], list)
