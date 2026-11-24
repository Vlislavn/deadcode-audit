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

from deadcode_audit import blind, clones, overlap_embed, overlaps
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
    monkeypatch.setattr(overlaps.diffscope, "runtime_files", lambda **_: [Path("src/m.py")])
    monkeypatch.setattr(overlaps.diffscope, "read_text", lambda _p: module_src)
    funcs = overlaps.build_corpus()
    by_symbol = {f.record.symbol: f for f in funcs}
    assert by_symbol["alpha"].source == ast.get_source_segment(module_src, ast.parse(module_src).body[0])
    assert by_symbol["beta"].source.startswith("def beta(x):")


def test_build_corpus_none_segment_falls_back_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    module_src = "def alpha():\n    return 1\n"
    monkeypatch.setattr(overlaps.diffscope, "runtime_files", lambda **_: [Path("src/m.py")])
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

    monkeypatch.setattr(overlaps, "build_corpus", lambda _paths=None: _CLI_FUNCS)
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


@pytest.mark.parametrize("stage", ["load", "run"])
def test_cli_any_embed_exception_falls_back_to_pass1_with_one_warning_naming_the_cause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stage: str
) -> None:
    # transformers 5.x: loading the pinned CodeSage remote code raises AttributeError, not ImportError/OSError.
    cause = "'CodeSageModel' object has no attribute 'all_tied_weights_keys'"

    def broken_embed(_sources: list[str]) -> np.ndarray:
        raise AttributeError(cause)

    def load(_model: str):
        if stage == "load":
            raise AttributeError(cause)
        return broken_embed

    _patch_corpus(monkeypatch, load)
    assert main(["overlaps", "--json", "--min-tokens", "1"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["embed_active"] is False and payload["model"] is None and payload["embed_pairs"] == []
    warnings = [line for line in captured.err.splitlines() if line.strip()]
    assert len(warnings) == 1 and warnings[0].startswith("warning:")
    assert "AttributeError" in warnings[0] and "all_tied_weights_keys" in warnings[0] and "transformers <5" in warnings[0]


def test_cli_no_embed_never_loads_model(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = _patch_corpus(monkeypatch, lambda _m: _fake_embed)
    assert main(["overlaps", "--no-embed", "--json", "--min-tokens", "1"]) == 0
    captured = capsys.readouterr()
    assert calls == [] and captured.err == ""
    assert json.loads(captured.out)["embed_active"] is False


# --- same-file containment applies to Python too ---


def _python_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> list[overlaps._SourcedFunc]:
    monkeypatch.setattr(overlaps.diffscope, "REPO_ROOT", tmp_path)
    (tmp_path / ".deadcode.yml").write_text("project:\n  source_roots: [src]\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(body, encoding="utf-8")
    return overlaps.build_corpus(["src"])


_NESTED = (
    "def get_item_notes(client, key):\n"
    "    def _read(note):\n"
    "        return client.fetch(note.id)\n"
    "    return [_read(n) for n in client.children(key)]\n"
    "\n\n"
    "def sibling(client, key):\n"
    "    def _read(note):\n"
    "        return client.fetch(note.id)\n"
    "    return [_read(n) for n in client.children(key)]\n"
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_python_method_and_its_nested_closure_are_not_an_overlap_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newline: str
) -> None:
    corpus = _python_corpus(tmp_path, monkeypatch, _NESTED.replace("\n", newline))
    pairs = overlaps.find_overlaps(corpus, threshold=0, min_tokens=1, semantic_threshold=0)
    line_pairs = [{p.a_line, p.b_line} for p in pairs]
    # Siblings (the two identical bodies in different functions) stay comparable.
    assert {2, 8} in line_pairs and {1, 7} in line_pairs
    assert {1, 2} not in line_pairs and {7, 8} not in line_pairs


def test_python_spans_are_utf8_byte_offsets_of_the_def(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "# é\ndef f(x):\n    return x\n"
    [func] = _python_corpus(tmp_path, monkeypatch, source)
    raw = source.encode("utf-8")
    assert raw[func.record.start_byte : func.record.end_byte].decode("utf-8") == "def f(x):\n    return x"


# --- a function and its own thin call site are not an overlap pair; callers with their own logic stay ---

_CALL_SITES = (
    "def fetch_thing(client, key, options):\n"
    "    rows = [client.get(key, option) for option in options if option]\n"
    "    return {name: value for name, value in rows if value is not None}\n"
    "\n\n"
    "def thin_caller(client, key):\n"
    "    return fetch_thing(client, key, ['a', 'b', 'c'])\n"
    "\n\n"
    "def busy_caller(client, key, items):\n"
    "    seen = []\n"
    "    for item in items:\n"
    "        if item.id in seen:\n"
    "            continue\n"
    "        seen.append(item.id)\n"
    "        client.touch(item.id, key, item.name)\n"
    "    total = sum(len(item.name) for item in items)\n"
    "    return fetch_thing(client, key, ['a']), total\n"
)


def test_python_thin_call_site_is_dropped_but_a_caller_with_own_logic_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _python_corpus(tmp_path, monkeypatch, _CALL_SITES)
    calls = {f.record.symbol: dict(f.record.calls).get("fetch_thing") for f in corpus}
    assert calls["thin_caller"] < 40 <= calls["busy_caller"]
    pairs = overlaps.find_overlaps(corpus, threshold=0, min_tokens=1, semantic_threshold=0)
    symbols = [{p.a_symbol, p.b_symbol} for p in pairs]
    assert {"fetch_thing", "thin_caller"} not in symbols
    assert {"fetch_thing", "busy_caller"} in symbols and {"thin_caller", "busy_caller"} in symbols


def test_python_test_paths_are_excluded_from_explicit_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(overlaps.diffscope, "REPO_ROOT", tmp_path)
    (tmp_path / ".deadcode.yml").write_text("project:\n  source_roots: [src]\n")
    (tmp_path / "src" / "tests").mkdir(parents=True)
    (tmp_path / "src" / "m.py").write_text("def kept(x):\n    return x\n")
    (tmp_path / "src" / "test_m.py").write_text("def skipped_by_name(x):\n    return x\n")
    (tmp_path / "src" / "tests" / "helper.py").write_text("def skipped_by_dir(x):\n    return x\n")
    assert [f.record.symbol for f in overlaps.build_corpus(["src"])] == ["kept"]


# --- identifier-blind second signal (V3iii) ---


def test_blind_python_a_renamed_copy_becomes_identical_but_literals_stay() -> None:
    original = "def total(items, rate):\n    return sum(i.price for i in items) * rate + 10\n"
    renamed = "def amount(rows, factor):\n    return sum(r.cost for r in rows) * factor + 10\n"
    other_literal = renamed.replace("10", "11")
    assert blind.python_blind(original) == blind.python_blind(renamed)
    assert blind.python_blind(original) == "def v0(v1, v2):\n    return v3(v4.v5 for v4 in v1) * v2 + 10\n"
    assert blind.python_blind(original) != blind.python_blind(other_literal)


def test_blind_python_same_identifiers_with_different_structure_stay_different() -> None:
    first = "def f(x, y):\n    return x + y\n"
    second = "def f(x, y):\n    return x * y if y else x\n"
    assert blind.python_blind(first) != blind.python_blind(second)


def test_blind_python_a_function_and_its_own_call_site_are_not_identical() -> None:
    callee = "def apply(plan, store):\n    store.write(plan.items, plan.mode)\n    return plan.mode\n"
    caller = "def run(plan, store):\n    return apply(plan, store)\n"
    assert blind.python_blind(callee) != blind.python_blind(caller)
    # sharing the callee's NAME is what made them look alike; without it the texts no longer contain it
    assert "apply" not in blind.python_blind(caller)


def test_blind_python_untokenizable_source_is_none() -> None:
    assert blind.python_blind("def f(:\n  '''") is None


def _blind_candidates(identifier_blind: bool) -> list[overlaps.OverlapPair]:
    # Plain texts are all identical (cosine 1.0 for every pair); blind texts sit at angles 0/10/25/60 degrees, so the
    # six blind cosines are all different: ab .985, bc .966, ac .906, cd .819, bd .643, ad .500; median .8625.
    angles = {"b0": 0, "b1": 10, "b2": 25, "b3": 60}

    def embed(sources: list[str], text_transform: str | None = None) -> np.ndarray:
        if text_transform is None:
            return np.tile([1.0, 0.0], (len(sources), 1))
        assert text_transform == blind.BLIND_ID
        return np.array([[np.cos(np.radians(angles[s])), np.sin(np.radians(angles[s]))] for s in sources])

    funcs = [
        overlaps._SourcedFunc(
            clones.FuncRecord(Path("src/x.py"), i + 1, name, _TOK, _SEM), source=f"p{i}", blind_source=f"b{i}"
        )
        for i, name in enumerate("abcd")
    ]
    return overlaps.find_embed_candidates(
        funcs, min_tokens=1, embed_fn=embed, known=[], topk=3, identifier_blind=identifier_blind
    )


def test_blind_median_rule_keeps_pairs_at_or_above_the_median_blind_cosine() -> None:
    kept = _blind_candidates(True)
    assert {(p.a_symbol, p.b_symbol) for p in kept} == {("a", "b"), ("b", "c"), ("a", "c")}
    assert all(p.embed == pytest.approx(1.0) for p in kept)  # the reported cosine stays the plain one
    assert len(_blind_candidates(False)) == 6
