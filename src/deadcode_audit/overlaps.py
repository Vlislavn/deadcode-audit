"""On-demand, advisory function-overlap audit: full all-pairs, ranked, semantic.

Eligible functions are compared as upper-triangle candidates, except strict same-file JS/TS
ancestor/descendant source-span pairs; every remaining pair is scored once. Each pair gets a hybrid
score = embedding cosine + structural token similarity + API-name Jaccard, ranked
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
import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from deadcode_audit import clones, config, diffscope, javascript, scan

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
_JS_FUNCTION_LANGUAGES = frozenset({"javascript", "typescript"})


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
            funcs.extend(_SourcedFunc(record, text) for record, text in collect_functions(source, path))
            continue
        source = diffscope.read_text(path)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tokens = tuple(clones.normalize_function(node))
                record = clones._FuncRecord(path, node.lineno, node.name, tokens, clones._semantic_signature(tokens))
                # Fail-open: a None segment (rare, position-less node) -> "" -> deterministic-only for this fn.
                funcs.append(_SourcedFunc(record=record, source=ast.get_source_segment(source, node) or ""))
    return funcs


def _input_identity(function: _SourcedFunc) -> dict:
    record = function.record
    raw = function.source.encode("utf-8")
    result = {"file": record.file_path.as_posix(), "line": record.line, "symbol": record.symbol,
              "language": record.language, "source_bytes": len(raw), "source_sha256": hashlib.sha256(raw).hexdigest()}
    if record.language != "python":
        result.update(column=record.column, start_byte=record.start_byte, end_byte=record.end_byte)
    return result


def _strict_same_file_js_containment(first: clones._FuncRecord, second: clones._FuncRecord) -> bool:
    """Return whether two valid JS/TS source spans strictly contain one another."""
    if first.language not in _JS_FUNCTION_LANGUAGES or second.language not in _JS_FUNCTION_LANGUAGES:
        return False
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


def _count_skipped_containment(funcs: list[_SourcedFunc], min_tokens: int) -> int:
    survivors = [function for function in funcs if len(function.record.tokens) >= min_tokens]
    return sum(
        _strict_same_file_js_containment(survivors[i].record, survivors[j].record)
        for i in range(len(survivors))
        for j in range(i + 1, len(survivors))
    )


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
    """Return ALL non-containment function pairs scoring >= ``threshold``, ranked descending.

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

    mixed_metadata = any(function.record.language != "python" for function in funcs)
    pairs: list[OverlapPair] = []
    for i in range(count):
        first = survivors[i]
        tokens_i, sem_i = first.record.tokens, first.record.semantic
        for j in range(i + 1, count):
            second = survivors[j]
            tokens_j, sem_j = second.record.tokens, second.record.semantic
            if _strict_same_file_js_containment(first.record, second.record):
                continue
            if first.record.language != second.record.language:
                continue

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
                    _input_identity(first) if mixed_metadata else None,
                    _input_identity(second) if mixed_metadata else None,
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
                "a": p.a_input or {"file": p.a_file, "line": p.a_line, "symbol": p.a_symbol},
                "b": p.b_input or {"file": p.b_file, "line": p.b_line, "symbol": p.b_symbol},
                "combined": p.combined,
                "struct": p.struct,
                "api": p.api,
                "embed": p.embed,
            }
            for p in pairs
        ],
    }
    return json.dumps(payload, indent=2)


def run(*, threshold: float, top: int, min_tokens: int, no_embed: bool, model: str, as_json: bool,
        paths: list[str] | None = None) -> int:
    """Build the corpus, run the all-pairs audit, render, and ALWAYS return 0 (advisory, never blocks)."""
    corpus = build_corpus(paths)
    if not corpus:
        raise ValueError("No executable functions found in selected source roots")
    eligible = sum(len(f.record.tokens) >= min_tokens for f in corpus)
    if not eligible:
        raise ValueError("No functions meet min_tokens; no comparisons performed")
    embed_fn: EmbedFn | None = None
    if not no_embed:
        from deadcode_audit import overlap_embed

        embed_fn = overlap_embed.get_embedder(model)
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
        if any(function.record.language != "python" for function in corpus):
            payload["functions_by_language"] = dict(Counter(function.record.language for function in corpus))
            payload["comparison_pools"] = "within-language; JavaScript and TypeScript share javascript"
            payload["pairs_skipped_same_file_containment"] = _count_skipped_containment(corpus, min_tokens)
            payload["function_inputs"] = [
                {**_input_identity(function), "tokens": len(function.record.tokens),
                 "eligible": len(function.record.tokens) >= min_tokens,
                 "embedded": embed_active and len(function.record.tokens) >= min_tokens}
                for function in corpus
            ]
            payload["embedded_by_language"] = dict(Counter(
                function.record.language for function in corpus
                if embed_active and len(function.record.tokens) >= min_tokens
            ))
        rendered = json.dumps(payload, indent=2)
    print(rendered)
    return 0
