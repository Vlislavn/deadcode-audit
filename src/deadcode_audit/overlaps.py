"""On-demand, advisory function-overlap audit: full all-pairs, two passes.

Pass 1 (deterministic, always runs): every function in ``src/`` is compared against every other
(upper-triangle, each pair once); score = 0.6 * structural token similarity + 0.4 * API-name Jaccard.
Pass 2 (CodeSage embed-only, default): each function's top-``_EMBED_TOPK`` cosine neighbours that pass 1 did
not already report are added as type-4 (same behaviour, different code) candidates, ranked by cosine.
The embedder is dependency-INJECTED (``overlap_embed.get_embedder``), so this module imports without
torch/numpy and tests pass a fake embedder. If the model or its extras are missing, ``run`` warns once and
returns pass 1 only. On-demand only — never a gate, ``run`` always returns 0.

Reuses the Tier 6 primitives in :mod:`deadcode_audit.clones` (tokens, structural similarity, the
API-surface Jaccard) and :mod:`deadcode_audit.diffscope` (the ``src`` corpus). The sound length
pre-filter is inlined so a passing pair is never dropped.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from deadcode_audit import clones, diffscope
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


def build_corpus() -> list[_SourcedFunc]:
    """Collect every src function with its fingerprint tokens AND source segment, in one parse pass."""
    funcs: list[_SourcedFunc] = []
    for path in diffscope.all_src_files():
        source = diffscope.read_text(path)
        tree = safe_parse(source)
        if tree is None:
            # One malformed file must not crash the whole audit; warn and skip it.
            print(f"warning: {path.as_posix()} does not parse; skipping overlap analysis", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tokens = tuple(clones.normalize_function(node))
                record = clones.FuncRecord(path, node.lineno, node.name, tokens, clones.semantic_signature(tokens))
                # Fail-open: a None segment (rare, position-less node) -> "" -> deterministic-only for this fn.
                funcs.append(_SourcedFunc(record=record, source=ast.get_source_segment(source, node) or ""))
    return funcs


def _combined(struct: float, api: float) -> float:
    return _W_STRUCT * struct + _W_API * api


def _pair(first: _SourcedFunc, second: _SourcedFunc, combined: float, struct: float, api: float,
          embed: float | None) -> OverlapPair:
    return OverlapPair(
        first.record.file_path.as_posix(), first.record.line, first.record.symbol,
        second.record.file_path.as_posix(), second.record.line, second.record.symbol,
        round(combined, 3), round(struct, 3), round(api, 3), round(embed, 3) if embed is not None else None,
    )


def _key(func: _SourcedFunc) -> tuple[str, int, str]:
    return func.record.file_path.as_posix(), func.record.line, func.record.symbol


def find_overlaps(
    funcs: list[_SourcedFunc],
    *,
    threshold: float,
    min_tokens: int,
    semantic_threshold: float = _SEMANTIC_THRESHOLD,
) -> list[OverlapPair]:
    """Pass 1: return ALL function pairs scoring >= ``threshold`` (struct + api), ranked descending."""
    survivors = [f for f in funcs if len(f.record.tokens) >= min_tokens]
    pairs: list[OverlapPair] = []
    for i, first in enumerate(survivors):
        tokens_i, sem_i = first.record.tokens, first.record.semantic
        for second in survivors[i + 1 :]:
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
                pairs.append(_pair(first, second, combined, struct, api, None))
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
    nearest = (-matrix).argsort(axis=1)[:, : min(topk, len(survivors) - 1)]
    reported = {frozenset(((p.a_file, p.a_line, p.a_symbol), (p.b_file, p.b_line, p.b_symbol))) for p in known}

    pairs: list[OverlapPair] = []
    seen: set[tuple[int, int]] = set()
    for i, row in enumerate(nearest):
        for j in (int(x) for x in row):
            low, high = min(i, j), max(i, j)
            if (low, high) in seen:
                continue
            seen.add((low, high))
            first, second = survivors[low], survivors[high]
            if frozenset((_key(first), _key(second))) in reported:
                continue
            api = clones.jaccard(first.record.semantic, second.record.semantic)
            struct = clones.clone_similarity(list(first.record.tokens), list(second.record.tokens))
            pairs.append(_pair(first, second, _combined(struct, api), struct, api, float(matrix[low, high])))
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
        "a": {"file": p.a_file, "line": p.a_line, "symbol": p.a_symbol},
        "b": {"file": p.b_file, "line": p.b_line, "symbol": p.b_symbol},
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
    except (ImportError, OSError) as error:
        reason = (str(error).splitlines() or [""])[0]
        print(f"warning: embedding pass skipped, {model} unavailable ({type(error).__name__}: {reason}); "
              "showing deterministic pass 1 only (install with `uv sync --extra embeddings`)", file=sys.stderr)
        return None


def run(*, threshold: float, top: int, min_tokens: int, no_embed: bool, model: str, as_json: bool) -> int:
    """Build the corpus, run pass 1 (+ pass 2 unless ``no_embed``/unavailable), render, ALWAYS return 0."""
    embed_fn = None if no_embed else _load_embedder(model)
    corpus = build_corpus()
    if not corpus:
        raise ValueError("No Python functions found in configured source roots")
    eligible = sum(len(f.record.tokens) >= min_tokens for f in corpus)
    if not eligible:
        raise ValueError("No functions meet min_tokens; no comparisons performed")
    all_pairs = find_overlaps(corpus, threshold=threshold, min_tokens=min_tokens)
    all_embed = (find_embed_candidates(corpus, min_tokens=min_tokens, embed_fn=embed_fn, known=all_pairs)
                 if embed_fn is not None else [])
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
        print(json.dumps(payload, indent=2))
    else:
        print(_render_human(pairs, embed_pairs, embed_active=embed_active, threshold=threshold, model=model))
    return 0
