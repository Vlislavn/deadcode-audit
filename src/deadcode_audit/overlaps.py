"""On-demand, advisory function-overlap audit: full all-pairs, two passes.

Pass 1 (deterministic, always runs): every function in ``src/`` (Python, plus JavaScript/TypeScript when the
``javascript`` extra is installed) is compared against every other within its language pool (upper-triangle,
each pair once; strict same-file ancestor/descendant source-span pairs, e.g. a method and its nested closure, are skipped);
score = 0.6 * structural token similarity + 0.4 * API-name Jaccard.
Pass 2 (CodeSage embed-only, default): each function's top-``_EMBED_TOPK`` cosine neighbours that pass 1 did
not already report are added as type-4 (same behaviour, different code) candidates, ranked by cosine.
The embedder is dependency-INJECTED (``overlap_embed.get_embedder``), so this module imports without
torch/numpy and tests pass a fake embedder. If the model or its extras are missing, or loading/running it fails for any reason, ``run`` warns once
and returns pass 1 only. On-demand only — never a gate, ``run`` always returns 0.

Reuses the Tier 6 primitives in :mod:`deadcode_audit.clones` (tokens, structural similarity, the
API-surface Jaccard) and :mod:`deadcode_audit.diffscope` (the ``src`` corpus). The sound length
pre-filter is inlined so a passing pair is never dropped.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from deadcode_audit import clones, config, diffscope, javascript, scan
from deadcode_audit.framework import safe_parse

if TYPE_CHECKING:
    import numpy as np

EmbedFn = Callable[[list[str]], "np.ndarray"]

# Fixed scoring weights (tool-owned constants, not user flags).
_W_STRUCT, _W_API = 0.6, 0.4

# API-surface Jaccard gate; lower than clones' 0.6 for recall. (CLI flag defaults — threshold 0.55,
# top 50, min-tokens 40, model — are the single source in cli.py's argparse; run() takes them explicitly.)
_SEMANTIC_THRESHOLD = 0.3

# Pass 2 keeps each function's top-3 cosine neighbours (the q162 v2 rule was evaluated at recall@3).
_EMBED_TOPK = 3


@dataclass(frozen=True)
class _SourcedFunc:
    """A clone fingerprint record plus the function's source text (for embedding)."""

    record: clones.FuncRecord
    source: str


@dataclass(frozen=True)
class OverlapPair:
    """One ranked overlapping pair with its per-signal sub-scores (``embed`` None == deterministic)."""

    a_file: str
    a_line: int
    a_symbol: str
    b_file: str
    b_line: int
    b_symbol: str
    combined: float
    struct: float
    api: float
    embed: float | None
    a_input: dict | None = None
    b_input: dict | None = None


def build_corpus(paths: list[str] | None = None) -> list[_SourcedFunc]:
    """Collect every src function with its fingerprint tokens AND source segment, in one parse pass."""
    funcs: list[_SourcedFunc] = []
    if paths:
        selected = scan.resolve_target_files(paths, changed=False, compare_branch="main",
                                             exclude=config.load_deadcode_config(diffscope.REPO_ROOT).exclude)
    else:
        python = diffscope.all_src_files()
        candidates = scan.resolve_target_files([], changed=False, compare_branch="main",
                                               exclude=config.load_deadcode_config(diffscope.REPO_ROOT).exclude)
        selected = [*python, *(path for path in candidates if path.suffix in javascript.SUFFIXES)]
    for path in selected:
        if path.suffix in javascript.SUFFIXES:
            from deadcode_audit.javascript_functions import collect_functions
            source = (diffscope.REPO_ROOT / path).read_bytes().decode("utf-8")
            try:
                funcs.extend(_SourcedFunc(record, text) for record, text in collect_functions(source, path))
            except SyntaxError:  # same policy as Python: one malformed file never aborts the audit
                print(f"warning: {path.as_posix()} does not parse; skipping overlap analysis", file=sys.stderr)
            continue
        source = diffscope.read_text(path)
        tree = safe_parse(source)
        if tree is None:
            # One malformed file must not crash the whole audit; warn and skip it.
            print(f"warning: {path.as_posix()} does not parse; skipping overlap analysis", file=sys.stderr)
            continue
        line_starts = _line_starts(source.encode("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tokens = tuple(clones.normalize_function(node))
                record = clones.FuncRecord(path, node.lineno, node.name, tokens, clones.semantic_signature(tokens),
                                           start_byte=line_starts[node.lineno - 1] + node.col_offset,
                                           end_byte=line_starts[node.end_lineno - 1] + node.end_col_offset)
                # Fail-open: a None segment (rare, position-less node) -> "" -> deterministic-only for this fn.
                funcs.append(_SourcedFunc(record=record, source=ast.get_source_segment(source, node) or ""))
    return funcs


def _line_starts(raw: bytes) -> list[int]:
    """Byte offset of each line start, splitting on the same \\n, \\r\\n and \\r that ``ast`` counts."""
    return [0, *(match.end() for match in re.finditer(rb"\r\n|\r|\n", raw))]


def _combined(struct: float, api: float) -> float:
    return _W_STRUCT * struct + _W_API * api


def _input_identity(function: _SourcedFunc) -> dict:
    record = function.record
    raw = function.source.encode("utf-8")
    result = {"file": record.file_path.as_posix(), "line": record.line, "symbol": record.symbol,
              "language": record.language, "source_bytes": len(raw), "source_sha256": hashlib.sha256(raw).hexdigest()}
    if record.language != "python":
        result.update(column=record.column, start_byte=record.start_byte, end_byte=record.end_byte)
    return result


def _strict_same_file_containment(first: clones.FuncRecord, second: clones.FuncRecord) -> bool:
    """Return whether two valid same-file function source spans strictly contain one another (any language)."""
    if first.file_path != second.file_path:
        return False
    spans = (first.start_byte, first.end_byte, second.start_byte, second.end_byte)
    if any(type(offset) is not int for offset in spans):
        return False
    first_start, first_end, second_start, second_end = spans
    if min(spans) < 0 or first_start >= first_end or second_start >= second_end:
        return False
    contained = (first_start <= second_start and second_end <= first_end) or (
        second_start <= first_start and first_end <= second_end
    )
    return contained and spans[:2] != spans[2:]


def _comparable(first: _SourcedFunc, second: _SourcedFunc) -> bool:
    """Pairs are compared within one language pool, never across a strict same-file containment."""
    return first.record.language == second.record.language and not _strict_same_file_containment(
        first.record, second.record
    )


def _count_skipped_containment(funcs: list[_SourcedFunc], min_tokens: int) -> int:
    survivors = [function for function in funcs if len(function.record.tokens) >= min_tokens]
    return sum(
        _strict_same_file_containment(survivors[i].record, survivors[j].record)
        for i in range(len(survivors))
        for j in range(i + 1, len(survivors))
    )


def _pair(first: _SourcedFunc, second: _SourcedFunc, combined: float, struct: float, api: float,
          embed: float | None, *, identities: bool = False) -> OverlapPair:
    return OverlapPair(
        first.record.file_path.as_posix(), first.record.line, first.record.symbol,
        second.record.file_path.as_posix(), second.record.line, second.record.symbol,
        round(combined, 3), round(struct, 3), round(api, 3), round(embed, 3) if embed is not None else None,
        _input_identity(first) if identities else None,
        _input_identity(second) if identities else None,
    )


def _key(func: _SourcedFunc) -> tuple[str, int, str, int | None]:
    record = func.record
    return record.file_path.as_posix(), record.line, record.symbol, record.start_byte


def _pair_keys(pair: OverlapPair) -> frozenset[tuple[str, int, str, int | None]]:
    """Both endpoints of a pair as ``_key`` tuples (JS/TS endpoints carry their byte offset)."""
    first = pair.a_input or {"file": pair.a_file, "line": pair.a_line, "symbol": pair.a_symbol}
    second = pair.b_input or {"file": pair.b_file, "line": pair.b_line, "symbol": pair.b_symbol}
    return frozenset((d["file"], d["line"], d["symbol"], d.get("start_byte")) for d in (first, second))


def find_overlaps(
    funcs: list[_SourcedFunc],
    *,
    threshold: float,
    min_tokens: int,
    semantic_threshold: float = _SEMANTIC_THRESHOLD,
) -> list[OverlapPair]:
    """Pass 1: return ALL comparable function pairs scoring >= ``threshold`` (struct + api), ranked descending."""
    survivors = [f for f in funcs if len(f.record.tokens) >= min_tokens]
    mixed_metadata = any(f.record.language != "python" for f in funcs)
    pairs: list[OverlapPair] = []
    for i, first in enumerate(survivors):
        tokens_i, sem_i = first.record.tokens, first.record.semantic
        for second in survivors[i + 1 :]:
            if not _comparable(first, second):
                continue
            tokens_j, sem_j = second.record.tokens, second.record.semantic

            # Sound length pre-filter: skip only when the MAX possible combined score (api at its
            # ceiling of 1.0, struct at its SequenceMatcher upper bound 2*min/(a+b)) is below
            # threshold — so a pair that could pass is never dropped.
            shorter, longer = sorted((len(tokens_i), len(tokens_j)))
            struct_ceiling = (2 * shorter) / (shorter + longer) if (shorter + longer) else 0.0
            if _W_API + _W_STRUCT * struct_ceiling < threshold:
                continue

            api = clones.jaccard(sem_i, sem_j)
            if api < semantic_threshold:
                continue
            struct = clones.clone_similarity(list(tokens_i), list(tokens_j))
            combined = _combined(struct, api)
            if combined >= threshold:
                pairs.append(_pair(first, second, combined, struct, api, None, identities=mixed_metadata))
    pairs.sort(key=lambda p: p.combined, reverse=True)
    return pairs


def find_embed_candidates(
    funcs: list[_SourcedFunc],
    *,
    min_tokens: int,
    embed_fn: EmbedFn,
    known: list[OverlapPair],
    topk: int = _EMBED_TOPK,
) -> list[OverlapPair]:
    """Pass 2 (embed-only): top-``topk`` cosine neighbours of each function that pass 1 did not report.

    Ranked by cosine, descending. ``combined``/``struct``/``api`` are the pass-1 sub-scores of the pair
    (it fell below threshold or was removed by the api gate), shown for context only.
    """
    survivors = [f for f in funcs if len(f.record.tokens) >= min_tokens and f.source.strip()]
    if len(survivors) < 2:
        return []
    vectors = embed_fn([f.source for f in survivors])  # L2-normalized rows (np.ndarray)
    matrix = vectors @ vectors.T  # cosine matrix; dot == cosine because rows are unit-norm
    matrix[range(len(survivors)), range(len(survivors))] = -2.0  # a function is not its own neighbour
    for i, first in enumerate(survivors):  # no neighbours across language pools or strict JS/TS containment
        for j in range(i + 1, len(survivors)):
            if not _comparable(first, survivors[j]):
                matrix[i, j] = matrix[j, i] = -2.0
    nearest = (-matrix).argsort(axis=1)[:, : min(topk, len(survivors) - 1)]
    reported = {_pair_keys(p) for p in known}
    mixed_metadata = any(f.record.language != "python" for f in funcs)

    pairs: list[OverlapPair] = []
    seen: set[tuple[int, int]] = set()
    for i, row in enumerate(nearest):
        for j in (int(x) for x in row):
            low, high = min(i, j), max(i, j)
            if (low, high) in seen:
                continue
            seen.add((low, high))
            if matrix[low, high] <= -2.0:  # masked: not comparable (other language pool / JS containment)
                continue
            first, second = survivors[low], survivors[high]
            if frozenset((_key(first), _key(second))) in reported:
                continue
            api = clones.jaccard(first.record.semantic, second.record.semantic)
            struct = clones.clone_similarity(list(first.record.tokens), list(second.record.tokens))
            pairs.append(_pair(first, second, _combined(struct, api), struct, api, float(matrix[low, high]),
                               identities=mixed_metadata))
    pairs.sort(key=lambda p: p.embed or 0.0, reverse=True)
    return pairs


def _pair_line(pair: OverlapPair) -> str:
    embed_str = str(pair.embed) if pair.embed is not None else "n/a"
    return (
        f"  {pair.combined:<6} {pair.a_file}:{pair.a_line} '{pair.a_symbol}'  <->  "
        f"{pair.b_file}:{pair.b_line} '{pair.b_symbol}'   "
        f"[struct {pair.struct} api {pair.api} embed {embed_str}]"
    )


def _render_human(pairs: list[OverlapPair], embed_pairs: list[OverlapPair], *, embed_active: bool, threshold: float,
                  model: str) -> str:
    detail = f"pass 2 model {model}" if embed_active else "pass 1 only, embed n/a"
    lines = [f"overlaps audit: {len(pairs)} pair(s) >= {threshold}  ({detail})"]
    lines += [_pair_line(p) for p in pairs] or ["  no overlapping pairs above threshold"]
    if embed_active:
        lines.append(f"embedding candidates not in pass 1: {len(embed_pairs)}  (ranked by cosine)")
        lines += [_pair_line(p) for p in embed_pairs] or ["  none"]
    return "\n".join(lines)


def _pair_json(p: OverlapPair) -> dict[str, object]:
    return {
        "a": p.a_input or {"file": p.a_file, "line": p.a_line, "symbol": p.a_symbol},
        "b": p.b_input or {"file": p.b_file, "line": p.b_line, "symbol": p.b_symbol},
        "combined": p.combined,
        "struct": p.struct,
        "api": p.api,
        "embed": p.embed,
    }


def _load_embedder(model: str) -> EmbedFn | None:
    """Pass-2 embedder, or None (with ONE warning line) when the model or its extras are unavailable."""
    from deadcode_audit import overlap_embed

    try:
        return overlap_embed.get_embedder(model)
    except Exception as error:  # noqa: BLE001 - any load failure (e.g. transformers 5.x) falls back to pass 1
        _warn_embed_skipped(model, error)
        return None


def _warn_embed_skipped(model: str, error: Exception) -> None:
    reason = (str(error).splitlines() or [""])[0]
    print(f"warning: embedding pass skipped, {model} unavailable ({type(error).__name__}: {reason}); "
          "showing deterministic pass 1 only (install with `uv sync --extra embeddings`; the pinned CodeSage "
          "remote code needs transformers <5)", file=sys.stderr)


def _language_payload(corpus: list[_SourcedFunc], *, min_tokens: int, embed_active: bool) -> dict[str, object]:
    """Extra JSON keys, present only when the corpus holds non-Python functions."""
    embedded = [f for f in corpus if embed_active and len(f.record.tokens) >= min_tokens]
    return {
        "functions_by_language": dict(Counter(f.record.language for f in corpus)),
        "comparison_pools": "within-language; JavaScript and TypeScript share javascript",
        "pairs_skipped_same_file_containment": _count_skipped_containment(corpus, min_tokens),
        "function_inputs": [
            {**_input_identity(f), "tokens": len(f.record.tokens),
             "eligible": len(f.record.tokens) >= min_tokens,
             "embedded": embed_active and len(f.record.tokens) >= min_tokens}
            for f in corpus
        ],
        "embedded_by_language": dict(Counter(f.record.language for f in embedded)),
    }


def run(*, threshold: float, top: int, min_tokens: int, no_embed: bool, model: str, as_json: bool,
        paths: list[str] | None = None) -> int:
    """Build the corpus, run pass 1 (+ pass 2 unless ``no_embed``/unavailable), render, ALWAYS return 0."""
    corpus = build_corpus(paths)  # before the model load: a parse refusal must not pay for it
    if not corpus:
        raise ValueError("No executable functions found in selected source roots")
    eligible = sum(len(f.record.tokens) >= min_tokens for f in corpus)
    if not eligible:
        raise ValueError("No functions meet min_tokens; no comparisons performed")
    embed_fn = None if no_embed else _load_embedder(model)
    all_pairs = find_overlaps(corpus, threshold=threshold, min_tokens=min_tokens)
    all_embed: list[OverlapPair] = []
    if embed_fn is not None:
        try:
            all_embed = find_embed_candidates(corpus, min_tokens=min_tokens, embed_fn=embed_fn, known=all_pairs)
        except Exception as error:  # noqa: BLE001 - a failing model run also falls back to pass 1
            _warn_embed_skipped(model, error)
            embed_fn = None
    pairs, embed_pairs = (all_pairs[:top], all_embed[:top]) if top > 0 else (all_pairs, all_embed)
    embed_active = embed_fn is not None
    if as_json:
        from deadcode_audit import overlap_embed

        payload: dict[str, object] = {
            "threshold": threshold,
            "model": model if embed_active else None,
            "embed_active": embed_active,
            "count": len(pairs),
            "pairs": [_pair_json(p) for p in pairs],
            "embed_count": len(embed_pairs),
            "embed_pairs": [_pair_json(p) for p in embed_pairs],
            "functions_scanned": len(corpus),
            "functions_embedded": eligible if embed_active else 0,
            "functions_compared": eligible,
            "pairs_above_threshold": len(all_pairs),
            "embed_pairs_total": len(all_embed),
            "min_tokens": min_tokens,
        }
        if embed_active:
            payload["embedding"] = overlap_embed.model_metadata()
        if any(f.record.language != "python" for f in corpus):
            payload.update(_language_payload(corpus, min_tokens=min_tokens, embed_active=embed_active))
        print(json.dumps(payload, indent=2))
    else:
        print(_render_human(pairs, embed_pairs, embed_active=embed_active, threshold=threshold, model=model))
    return 0
