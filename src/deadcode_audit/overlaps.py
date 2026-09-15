"""On-demand, advisory function-overlap audit: full all-pairs, ranked, semantic.

Every function in ``src/`` is compared against every other (upper-triangle, so each pair once). Each
pair gets a hybrid score = embedding cosine + structural token similarity + API-name Jaccard, ranked
most-similar first; a function overlapping several others appears in every one of its pairs (not
best-match-only). The embedder is dependency-INJECTED (``overlap_embed.get_embedder``), so this module
imports without torch/numpy and tests pass a fake embedder; when the embedder is absent the score
degrades to deterministic ``struct + api``. This is on-demand only — never a gate, ``run`` always returns 0.

Reuses the Tier 6 primitives in :mod:`deadcode_audit.clones` (tokens, structural similarity, the
API-surface Jaccard) and :mod:`deadcode_audit.diffscope` (the ``src`` corpus). The sound length
pre-filter is inlined (with an embed-inclusive ceiling) so a passing pair is never dropped.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from deadcode_audit import clones, diffscope

if TYPE_CHECKING:
    import numpy as np

EmbedFn = Callable[[list[str]], "np.ndarray"]

# Fixed scoring weights (tool-owned constants, not user flags). Embedding is weighted highest — it is
# the signal that catches *semantic* overlap the deterministic signals miss.
_W_EMBED, _W_STRUCT, _W_API = 0.5, 0.3, 0.2
_W_STRUCT_DET, _W_API_DET = 0.6, 0.4

# API-surface Jaccard gate; lower than clones' 0.6 for recall. (CLI flag defaults — threshold 0.55,
# top 50, min-tokens 40, model — are the single source in cli.py's argparse; run() takes them explicitly.)
_SEMANTIC_THRESHOLD = 0.3


@dataclass(frozen=True)
class _SourcedFunc:
    """A clone fingerprint record plus the function's source text (for embedding)."""

    record: clones._FuncRecord
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
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tokens = tuple(clones.normalize_function(node))
                record = clones._FuncRecord(path, node.lineno, node.name, tokens, clones._semantic_signature(tokens))
                # Fail-open: a None segment (rare, position-less node) -> "" -> deterministic-only for this fn.
                funcs.append(_SourcedFunc(record=record, source=ast.get_source_segment(source, node) or ""))
    return funcs


def _combined_with_embed(struct: float, api: float, embed: float) -> float:
    return _W_EMBED * embed + _W_STRUCT * struct + _W_API * api


def _combined_deterministic(struct: float, api: float) -> float:
    return _W_STRUCT_DET * struct + _W_API_DET * api


def find_overlaps(
    funcs: list[_SourcedFunc],
    *,
    threshold: float,
    min_tokens: int,
    embed_fn: EmbedFn | None,
    semantic_threshold: float = _SEMANTIC_THRESHOLD,
) -> list[OverlapPair]:
    """Return ALL function pairs scoring >= ``threshold``, ranked descending (the injected-scoring core).

    Pure: ``embed_fn`` is injected (None => deterministic-only), so this needs neither torch nor an
    import of numpy (the cosine matrix is the ndarray ``embed_fn`` returns).
    """
    survivors = [f for f in funcs if len(f.record.tokens) >= min_tokens]
    count = len(survivors)

    matrix = None
    embeddable = [False] * count
    if embed_fn is not None and count:
        vectors = embed_fn([f.source for f in survivors])  # L2-normalized rows (np.ndarray)
        matrix = vectors @ vectors.T  # cosine matrix; dot == cosine because rows are unit-norm
        embeddable = [bool(f.source.strip()) for f in survivors]

    pairs: list[OverlapPair] = []
    for i in range(count):
        first = survivors[i]
        tokens_i, sem_i = first.record.tokens, first.record.semantic
        for j in range(i + 1, count):
            second = survivors[j]
            tokens_j, sem_j = second.record.tokens, second.record.semantic

            # Sound length pre-filter: skip only when the MAX possible combined score (other terms at
            # their ceiling of 1.0, struct at its SequenceMatcher upper bound 2*min/(a+b)) is below
            # threshold — so a pair that could pass is never dropped.
            shorter, longer = sorted((len(tokens_i), len(tokens_j)))
            struct_ceiling = (2 * shorter) / (shorter + longer) if (shorter + longer) else 0.0
            use_embed = matrix is not None and embeddable[i] and embeddable[j]
            if use_embed:
                if _W_EMBED + _W_API + _W_STRUCT * struct_ceiling < threshold:
                    continue
            elif _W_API_DET + _W_STRUCT_DET * struct_ceiling < threshold:
                continue

            api = clones._jaccard(sem_i, sem_j)
            if api < semantic_threshold:
                continue
            struct = clones.clone_similarity(list(tokens_i), list(tokens_j))

            if use_embed:
                assert matrix is not None  # use_embed implies the cosine matrix was built
                embed_value = float(matrix[i, j])
                embed: float | None = embed_value
                combined = _combined_with_embed(struct, api, embed_value)
            else:
                embed = None
                combined = _combined_deterministic(struct, api)
            if combined < threshold:
                continue
            pairs.append(
                OverlapPair(
                    first.record.file_path.as_posix(),
                    first.record.line,
                    first.record.symbol,
                    second.record.file_path.as_posix(),
                    second.record.line,
                    second.record.symbol,
                    round(combined, 3),
                    round(struct, 3),
                    round(api, 3),
                    round(embed, 3) if embed is not None else None,
                )
            )

    pairs.sort(key=lambda p: p.combined, reverse=True)
    return pairs


def _render_human(pairs: list[OverlapPair], *, embed_active: bool, threshold: float, model: str) -> str:
    detail = f"model {model}" if embed_active else "deterministic-only, embed n/a"
    lines = [f"overlaps audit: {len(pairs)} pair(s) >= {threshold}  ({detail})"]
    if not pairs:
        lines.append("  no overlapping pairs above threshold")
        return "\n".join(lines)
    for pair in pairs:
        embed_str = str(pair.embed) if pair.embed is not None else "n/a"
        lines.append(
            f"  {pair.combined:<6} {pair.a_file}:{pair.a_line} '{pair.a_symbol}'  <->  "
            f"{pair.b_file}:{pair.b_line} '{pair.b_symbol}'   "
            f"[struct {pair.struct} api {pair.api} embed {embed_str}]"
        )
    return "\n".join(lines)


def _render_json(pairs: list[OverlapPair], *, embed_active: bool, threshold: float, model: str) -> str:
    payload = {
        "threshold": threshold,
        "model": model if embed_active else None,
        "embed_active": embed_active,
        "count": len(pairs),
        "pairs": [
            {
                "a": {"file": p.a_file, "line": p.a_line, "symbol": p.a_symbol},
                "b": {"file": p.b_file, "line": p.b_line, "symbol": p.b_symbol},
                "combined": p.combined,
                "struct": p.struct,
                "api": p.api,
                "embed": p.embed,
            }
            for p in pairs
        ],
    }
    return json.dumps(payload, indent=2)


def run(*, threshold: float, top: int, min_tokens: int, no_embed: bool, model: str, as_json: bool) -> int:
    """Build the corpus, run the all-pairs audit, render, and ALWAYS return 0 (advisory, never blocks)."""
    embed_fn: EmbedFn | None = None
    if not no_embed:
        from deadcode_audit import overlap_embed

        embed_fn = overlap_embed.get_embedder(model)
    corpus = build_corpus()
    if not corpus:
        raise ValueError("No Python functions found in configured source roots")
    eligible = sum(len(f.record.tokens) >= min_tokens for f in corpus)
    if not eligible:
        raise ValueError("No functions meet min_tokens; no comparisons performed")
    pairs = find_overlaps(corpus, threshold=threshold, min_tokens=min_tokens, embed_fn=embed_fn)
    total_pairs = len(pairs)
    if top:
        pairs = pairs[:top]
    embed_active = embed_fn is not None
    rendered = (
        _render_json(pairs, embed_active=embed_active, threshold=threshold, model=model)
        if as_json
        else _render_human(pairs, embed_active=embed_active, threshold=threshold, model=model)
    )
    if as_json:
        payload = json.loads(rendered)
        payload["functions_scanned"] = len(corpus)
        payload["functions_embedded"] = eligible if embed_active else 0
        payload["functions_compared"] = eligible
        payload["pairs_above_threshold"] = total_pairs
        payload["min_tokens"] = min_tokens
        if embed_active:
            payload["embedding"] = overlap_embed.model_metadata()
        rendered = json.dumps(payload, indent=2)
    print(rendered)
    return 0
