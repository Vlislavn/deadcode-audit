"""Tier 6b: near-duplicate function detection (structural fingerprint + similarity).

Structurally-normalised function fingerprints compared by token-sequence similarity
(Type-1/2/3 clone detection), gated by a semantic-surface (called names/attributes) Jaccard
so two functions that merely share a control-flow skeleton but call disjoint APIs do not match.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from deadcode_audit.framework import safe_parse

if TYPE_CHECKING:
    from pathlib import Path

_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class CloneFinding:
    """A near-duplicate function pair."""

    file_path: Path
    line: int
    symbol: str
    other_file: Path
    other_line: int
    other_symbol: str
    similarity: float


def _local_binding_names(node: _FuncDef) -> set[str]:
    """Names bound locally in a function: parameters + anything assigned/iterated/with-as,
    except-as names, nested def/class names, and nested def/lambda parameters.

    The last three bind through plain ``str`` attributes on their AST nodes (``ExceptHandler.name``,
    ``FunctionDef.name``, nested ``ast.arg``), not Store-context ``Name`` nodes, so they must be
    collected explicitly — otherwise a clone differing only in one of those names scored below 1.0.
    Free names (called functions, imported symbols, module globals) are deliberately NOT included,
    so two functions that call *different* APIs do not look like clones.
    """
    names: set[str] = set()
    args = node.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        names.add(arg.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            names.add(sub.id)
        elif isinstance(sub, ast.arg):
            # Nested def/lambda parameters (the outer's own args were added above).
            names.add(sub.arg)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub is not node:
            # Skip the walked root itself so the function's own name keeps its free-name status
            # (a self-recursive call stays part of the semantic signature).
            names.add(sub.name)
    return names


def normalize_function(node: _FuncDef) -> list[str]:
    """Serialise a function body to a structure-only token stream for clone comparison.

    Local/argument identifiers are abstracted to positional placeholders (so renamed copies
    match — Type-2 clones), while control-flow shape, operators, literal *types*, and — crucially
    — **free names** (called functions, attributes, imported symbols) are preserved, so functions
    that merely share boilerplate structure but call different APIs do NOT collide. Docstring dropped.
    """
    locals_ = _local_binding_names(node)
    placeholders: dict[str, str] = {}

    def placeholder(name: str) -> str:
        return placeholders.setdefault(name, f"V{len(placeholders)}")

    tokens: list[str] = []
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop docstring
    for child in body:
        for sub in ast.walk(child):
            tokens.append(type(sub).__name__)
            if isinstance(sub, ast.Name):
                tokens.append(placeholder(sub.id) if sub.id in locals_ else f"#{sub.id}")
            elif isinstance(sub, ast.arg):
                tokens.append(placeholder(sub.arg))
            elif isinstance(sub, ast.Attribute):
                tokens.append(f".{sub.attr}")
            elif isinstance(sub, ast.Constant):
                tokens.append(f"<{type(sub.value).__name__}>")
            elif isinstance(sub, (ast.BinOp, ast.UnaryOp, ast.BoolOp)):
                tokens.append(type(sub.op).__name__)
            elif isinstance(sub, ast.Compare):
                tokens.extend(type(op).__name__ for op in sub.ops)
    return tokens


def clone_similarity(a: list[str], b: list[str]) -> float:
    """Token-sequence similarity in [0, 1] (1.0 == structurally identical).

    ``autojunk=False`` keeps the heuristic honest: the default ``autojunk=True`` silently treats
    any token occurring in >1% of a >=200-token sequence as junk, so long functions full of
    common tokens (``Name``, ``Load``, ...) get inflated ratios. Token lists here are exactly
    that shape, so junk heuristics would distort exactly the long-function comparisons the
    length pre-filter lets through.
    """
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def semantic_signature(tokens: tuple[str, ...]) -> frozenset[str]:
    """Free names (``#name``) and attribute/method names (``.attr``) — the *semantic* surface.

    Two functions can share a control-flow skeleton yet be unrelated; requiring overlap here
    means clones must also touch the same APIs/fields, not just the same shape.
    """
    return frozenset(token for token in tokens if token[:1] in "#.")


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class FuncRecord:
    """A function's clone fingerprint: location, normalised token stream, semantic surface."""

    file_path: Path
    line: int
    symbol: str
    tokens: tuple[str, ...]
    semantic: frozenset[str]


def collect_functions(source: str, path: Path) -> list[FuncRecord]:
    """Return a fingerprint record for every top-level and nested function definition.

    An unparseable file yields no records (with a stderr warning) instead of crashing the caller.
    """
    tree = safe_parse(source)
    if tree is None:
        print(f"warning: {path.as_posix()} does not parse; skipping clone fingerprinting", file=sys.stderr)
        return []
    records: list[FuncRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            tokens = tuple(normalize_function(node))
            records.append(FuncRecord(path, node.lineno, node.name, tokens, semantic_signature(tokens)))
    return records


def find_near_duplicate_functions(
    targets: list[FuncRecord],
    corpus: list[FuncRecord],
    *,
    threshold: float,
    min_tokens: int,
    semantic_threshold: float = 0.6,
) -> list[CloneFinding]:
    """Flag each target function that is a structural AND semantic near-duplicate of another.

    A match requires BOTH structural-token similarity >= ``threshold`` AND semantic-surface
    (free names / attributes) Jaccard >= ``semantic_threshold`` — so two functions that share a
    control-flow skeleton but call disjoint APIs are not reported. ``targets`` are the (changed)
    functions; ``corpus`` is the full comparison set. Sub-``min_tokens`` functions are skipped.
    Only the single best distinct match per target is reported.
    """
    findings: list[CloneFinding] = []
    for target in targets:
        if len(target.tokens) < min_tokens:
            continue
        best: tuple[float, FuncRecord] | None = None
        for other in corpus:
            if other is target or len(other.tokens) < min_tokens:
                continue
            if (other.file_path, other.line, other.symbol) == (target.file_path, target.line, target.symbol):
                continue
            # length pre-filter using the SOUND upper bound on SequenceMatcher.ratio():
            # ratio <= 2*min_len/(len_a+len_b). Skipping below this can never drop a pair that
            # would have scored >= threshold (the old ``shorter/longer < threshold`` was too
            # aggressive and dropped Type-3 clones with a few extra statements).
            shorter, longer = sorted((len(target.tokens), len(other.tokens)))
            if shorter + longer and (2 * shorter) / (shorter + longer) < threshold:
                continue
            if jaccard(target.semantic, other.semantic) < semantic_threshold:
                continue
            score = clone_similarity(list(target.tokens), list(other.tokens))
            if score >= threshold and (best is None or score > best[0]):
                best = (score, other)
        if best is not None:
            findings.append(
                CloneFinding(
                    target.file_path,
                    target.line,
                    target.symbol,
                    best[1].file_path,
                    best[1].line,
                    best[1].symbol,
                    round(best[0], 3),
                )
            )
    return findings
