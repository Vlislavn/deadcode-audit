"""Mixed-language overlap wiring; fake vectors are construction floors, not accuracy evidence."""

from __future__ import annotations

import builtins
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from deadcode_audit import clones, diffscope, overlap_embed, overlaps
from deadcode_audit.cli import main


def sources(tmp_path, monkeypatch):
    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (tmp_path / ".deadcode.yml").write_text("project:\n  source_roots: [src]\n")
    (source / "a.py").write_text("def invoke(value):\n    return api.echo(value)\n")
    (source / "b.js").write_text("module.exports = (value) => api.echo(value);\n")
    (source / "c.ts").write_text(
        "export const invoke = (item: number) => api.echo(item);\n"
    )
    return source


def test_default_and_explicit_corpus_collect_js_ts_and_python(tmp_path, monkeypatch):
    sources(tmp_path, monkeypatch)
    default = overlaps.build_corpus()
    explicit = overlaps.build_corpus(["src"])
    assert {function.record.language for function in default} == {
        "python",
        "javascript",
    }
    assert {(function.record.file_path, function.source) for function in default} == {
        (function.record.file_path, function.source) for function in explicit
    }
    assert len(default) == 3


def test_bom_and_crlf_offsets_are_relative_to_actual_captured_bytes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    raw = b"\xef\xbb\xbfconst marker = 1;\r\nmodule.exports = (value) => {\r\n  return api.echo(value);\r\n};\r\n"
    (tmp_path / "entry.cjs").write_bytes(raw)
    [function] = overlaps.build_corpus(["entry.cjs"])
    record = function.record
    assert record.line == 2
    assert raw[record.start_byte : record.end_byte] == function.source.encode()
    assert "\r\n" in function.source


def test_no_embed_compares_js_with_ts_but_never_python_with_js(
    tmp_path, monkeypatch, capsys
):
    sources(tmp_path, monkeypatch)
    assert (
        main(
            [
                "overlaps",
                "src",
                "--no-embed",
                "--json",
                "--top",
                "0",
                "--min-tokens",
                "1",
                "--threshold",
                "0",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["functions_by_language"] == {"python": 1, "javascript": 2}
    assert report["embedded_by_language"] == {} and report["functions_embedded"] == 0
    assert len(report["pairs"]) == 1
    pair = report["pairs"][0]
    assert pair["a"]["language"] == pair["b"]["language"] == "javascript"
    assert {pair["a"]["file"], pair["b"]["file"]} == {"src/b.js", "src/c.ts"}
    assert pair["embed"] is None


def test_actual_source_slices_are_passed_to_the_shared_embedder_with_counts_and_hashes(
    tmp_path, monkeypatch, capsys
):
    source = sources(tmp_path, monkeypatch)
    text = "const π = '🙂'; invoke(x => api.echo(x), y => api.echo(y));\n"
    (source / "b.js").write_text(text)
    observed = []

    def embed(snippets):
        observed.extend(snippets)
        return np.tile(np.array([1.0, 0.0]), (len(snippets), 1))

    monkeypatch.setattr(overlap_embed, "get_embedder", lambda _model: embed)
    monkeypatch.setattr(
        overlap_embed,
        "model_metadata",
        lambda: {"model": "fixture-wiring-floor", "revision": "not-real-model"},
    )
    assert (
        main(
            [
                "overlaps",
                "src",
                "--json",
                "--top",
                "0",
                "--min-tokens",
                "1",
                "--threshold",
                "0",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert (
        report["functions_scanned"]
        == report["functions_compared"]
        == report["functions_embedded"]
        == 4
    )
    assert report["embedded_by_language"] == {"python": 1, "javascript": 3}
    assert len(observed) == len(report["function_inputs"]) == 4
    for snippet, identity in zip(observed, report["function_inputs"], strict=True):
        assert hashlib.sha256(snippet.encode()).hexdigest() == identity["source_sha256"]
        assert (
            len(snippet.encode()) == identity["source_bytes"]
            and identity["embedded"] is True
        )
        if identity["language"] == "javascript":
            captured = (tmp_path / identity["file"]).read_bytes()
            assert (
                captured[identity["start_byte"] : identity["end_byte"]]
                == snippet.encode()
            )
    arrows = [row for row in report["function_inputs"] if row["file"] == "src/b.js"]
    assert arrows[0]["line"] == arrows[1]["line"] == 1
    assert arrows[0]["column"] != arrows[1]["column"]
    assert arrows[0]["start_byte"] != arrows[1]["start_byte"]
    assert all(
        pair["a"]["language"] == pair["b"]["language"] for pair in report["pairs"]
    )


def test_unparseable_js_file_is_skipped_with_a_warning_naming_it_and_the_model_still_runs(
    tmp_path, monkeypatch, capsys
):
    source = sources(tmp_path, monkeypatch)
    (source / "broken.js").write_text(
        "function earlier() { return 1; }\nfunction broken( {\n"
    )
    calls = []

    def get_embedder(model):
        calls.append(model)
        return lambda texts: np.eye(len(texts))

    monkeypatch.setattr(overlap_embed, "get_embedder", get_embedder)
    monkeypatch.setattr(overlap_embed, "model_metadata", lambda: {"model": "fake"})
    assert overlaps.run(
        paths=["src"], threshold=0, top=0, min_tokens=1, no_embed=False, model="m", as_json=True
    ) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert calls == ["m"] and payload["embed_active"] is True
    assert "earlier" not in captured.out  # the whole broken file is skipped, not partially read
    assert [line for line in captured.err.splitlines() if "broken.js" in line] == [
        "warning: src/broken.js does not parse; skipping overlap analysis"
    ]
    assert payload["functions_scanned"] == 3


def test_jsx_with_bare_ampersands_is_analysed_like_valid_jsx(tmp_path, monkeypatch, capsys):
    source = sources(tmp_path, monkeypatch)
    jsx = "export const A = () => <li>Readiness & calibration && more</li>;\n"
    (source / "d.jsx").write_text(jsx)
    assert overlaps.run(
        paths=["src"], threshold=0, top=0, min_tokens=1, no_embed=True, model="m", as_json=True
    ) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    [entry] = [f for f in payload["function_inputs"] if f["file"] == "src/d.jsx"]
    assert entry["source_sha256"] == hashlib.sha256(jsx[len("export const A = ") : -2].encode()).hexdigest()
    assert "does not parse" not in captured.err


def test_python_only_does_not_require_the_js_extra_but_js_refuses_missing_parser(
    tmp_path, monkeypatch
):
    sources(tmp_path, monkeypatch)
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith("tree_sitter"):
            raise ImportError("fixture missing optional parser")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert len(overlaps.build_corpus(["src/a.py"])) == 1
    with pytest.raises(ImportError):
        overlaps.build_corpus(["src/b.js"])


def test_python_only_json_keeps_its_existing_shape(tmp_path, monkeypatch, capsys):
    sources(tmp_path, monkeypatch)
    assert (
        main(["overlaps", "src/a.py", "--no-embed", "--json", "--min-tokens", "1"]) == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert "functions_by_language" not in report and "function_inputs" not in report
    assert "pairs_skipped_same_file_containment" not in report
    assert report["functions_scanned"] == report["functions_compared"] == 1


def test_function_record_identity_and_language_prevent_false_self_exclusion_and_cross_pool_clones():
    same = ("Return", "#api")
    semantic = frozenset({"#api"})
    first = clones.FuncRecord(
        Path("x.js"), 1, "nested", same, semantic, "javascript", 1, 0, 10
    )
    second = clones.FuncRecord(
        Path("x.js"), 1, "nested", same, semantic, "javascript", 12, 11, 21
    )
    python = clones.FuncRecord(Path("x.py"), 1, "nested", same, semantic)
    findings = clones.find_near_duplicate_functions(
        [first], [first, second, python], threshold=0, min_tokens=1
    )
    assert len(findings) == 1 and findings[0].other_file == Path("x.js")


def _source_identity(identity):
    return identity["file"], identity["start_byte"], identity["end_byte"]


def test_same_file_containment_is_filtered_before_scoring_but_siblings_and_other_files_remain(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "src"
    source.mkdir()
    (tmp_path / ".deadcode.yml").write_text("project:\n  source_roots: [src]\n")
    text = (
        "function outer(value) {\n"
        "  function inner(value) { return api.send(value); }\n"
        "  return api.send(value);\n"
        "}\n"
        "function siblingOne(value) { return api.send(value); }\n"
        "function siblingTwo(value) { return api.send(value); }\n"
    )
    (source / "first.js").write_text(text)
    (source / "second.js").write_text(text)
    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    corpus = overlaps.build_corpus(["src"])
    assert len(corpus) == 8

    parent_child = set()
    siblings = set()
    outer_by_file = {}
    for file in ("src/first.js", "src/second.js"):
        local = [function for function in corpus if function.record.file_path.as_posix() == file]
        outer = min(local, key=lambda function: function.record.start_byte)
        inner = next(
            function
            for function in local
            if function.record.start_byte > outer.record.start_byte
            and function.record.end_byte < outer.record.end_byte
        )
        sibling_functions = [function for function in local if function not in {outer, inner}]
        assert len(sibling_functions) == 2
        parent_child.add(
            frozenset(
                (
                    (file, outer.record.start_byte, outer.record.end_byte),
                    (file, inner.record.start_byte, inner.record.end_byte),
                )
            )
        )
        siblings.add(
            frozenset(
                (file, function.record.start_byte, function.record.end_byte)
                for function in sibling_functions
            )
        )
        outer_by_file[file] = (file, outer.record.start_byte, outer.record.end_byte)

    observed_embeddings = []
    scored_pairs = []
    original_similarity = clones.clone_similarity

    def embed(snippets):
        observed_embeddings.extend(snippets)
        return np.tile(np.array([1.0, 0.0]), (len(snippets), 1))

    def score(a, b):
        scored_pairs.append((a, b))
        return original_similarity(a, b)

    monkeypatch.setattr(clones, "clone_similarity", score)
    for functions in (corpus, list(reversed(corpus))):
        scored_pairs.clear()
        pairs = overlaps.find_overlaps(
            functions,
            threshold=0,
            min_tokens=1,
            semantic_threshold=0,
        )
        pair_identities = {
            frozenset((_source_identity(pair.a_input), _source_identity(pair.b_input)))
            for pair in pairs
        }
        assert len(pairs) == 26  # 28 possible pairs minus the two nested parent/child pairs.
        assert len(scored_pairs) == 26
        assert parent_child.isdisjoint(pair_identities)
        assert siblings <= pair_identities
        assert frozenset(outer_by_file.values()) in pair_identities

        # Pass 2 (embed-only) neighbours obey the same comparability rule as pass 1.
        candidates = overlaps.find_embed_candidates(
            functions, min_tokens=1, embed_fn=embed, known=[], topk=len(functions)
        )
        candidate_identities = {
            frozenset((_source_identity(pair.a_input), _source_identity(pair.b_input)))
            for pair in candidates
        }
        assert candidate_identities == pair_identities

    assert len(observed_embeddings) == 16  # Every function remains an embedding input in both runs.

    assert (
        main(
            [
                "overlaps",
                "src",
                "--no-embed",
                "--json",
                "--top",
                "0",
                "--min-tokens",
                "1",
                "--threshold",
                "0",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["functions_scanned"] == 8
    assert len(report["function_inputs"]) == 8
    assert report["pairs_skipped_same_file_containment"] == 2


@pytest.mark.parametrize(
    ("first_span", "second_span"),
    [
        ((None, 20), (11, 19)),
        ((True, 20), (11, 19)),
        ((10, 10), (11, 19)),
        ((10, 20), (10, 20)),
        ((20, 10), (11, 19)),
    ],
)
def test_invalid_or_non_strict_js_spans_remain_candidates(first_span, second_span):
    tokens = ("Identifier", "#api")
    semantic = frozenset({"#api"})
    first = overlaps._SourcedFunc(
        clones.FuncRecord(
            Path("src/a.js"),
            1,
            "outer",
            tokens,
            semantic,
            "javascript",
            1,
            *first_span,
        ),
        "same body",
    )
    second = overlaps._SourcedFunc(
        clones.FuncRecord(
            Path("src/a.js"),
            2,
            "inner",
            tokens,
            semantic,
            "javascript",
            1,
            *second_span,
        ),
        "same body",
    )
    pairs = overlaps.find_overlaps(
        [first, second],
        threshold=0,
        min_tokens=1,
        semantic_threshold=0,
    )
    assert len(pairs) == 1


@pytest.mark.parametrize("first_span,second_span", [((1, 20), (4, 20)), ((1, 20), (1, 16))])
def test_proper_containment_can_share_one_boundary(first_span, second_span):
    def record(span):
        return clones.FuncRecord(Path("entry.js"), 1, "arrow", ("Return",), frozenset(),
                                  "javascript", 1, *span)
    assert overlaps._strict_same_file_containment(record(first_span), record(second_span))
    assert overlaps._strict_same_file_containment(record(second_span), record(first_span))


def test_python_functions_without_byte_spans_keep_their_existing_candidate_behavior():
    tokens = ("Return", "#api")
    semantic = frozenset({"#api"})
    first = overlaps._SourcedFunc(
        clones.FuncRecord(Path("src/outer.py"), 1, "outer", tokens, semantic),
        "same body",
    )
    second = overlaps._SourcedFunc(
        clones.FuncRecord(Path("src/outer.py"), 2, "inner", tokens, semantic),
        "same body",
    )
    pairs = overlaps.find_overlaps(
        [first, second],
        threshold=0,
        min_tokens=1,
        semantic_threshold=0,
    )
    assert len(pairs) == 1
