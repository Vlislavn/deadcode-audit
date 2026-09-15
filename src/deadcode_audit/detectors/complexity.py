"""Complexity / size detectors: oversized functions, oversized files, deep nesting, param bloat.

These four rules encode *structural* maintainability principles that AI-generated code routinely
violates: monolithic functions, sprawling files, arrow-shaped nesting, and parameter-list bloat.
Every threshold is a module-level constant (tunable, not an inline magic literal), and every rule is
biased toward false negatives — when a measurement is ambiguous we under-count rather than over-flag.

The ``function-too-long`` measurement is *logical*: it counts only the body source lines that carry
real code, excluding the signature, the docstring, blank lines, and comment-only lines. That way a
function that is long only because it is well-documented or spaced out is not penalised — the rule
fires on genuine logic volume, not on prose.
"""

from __future__ import annotations

import ast
import io
import tokenize
from typing import TYPE_CHECKING

from deadcode_audit.diagnostic import (
    ENGINE_CODE_QUALITY,
    Diagnostic,
    RuleSpec,
    Severity,
    diagnostic_from_spec,
)

if TYPE_CHECKING:
    from deadcode_audit.framework import FileContext

# --- Tunable thresholds (module-level constants, never inline magic literals) ---

FUNCTION_MAX_LINES = 80  # logical body lines (excl. signature/docstring/blank/comment-only)
FILE_MAX_LINES = (
    500  # total physical lines — aligned to the source monorepo's own file-size gate (scripts/check_file_size.py, 500 SLOC)
)
MAX_NESTING = 5  # control-flow nesting depth (if/for/while/with/try bodies)
MAX_PARAMS = 6  # required parameters (excl. self/cls, *args/**kwargs, bare * and /, and defaulted params)

# Statement node types whose *body* (and handlers/orelse/finalbody) introduce a new nesting level.
_NESTING_STMTS: tuple[type[ast.stmt], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)

_FUNC_TYPES: tuple[type[ast.stmt], ...] = (ast.FunctionDef, ast.AsyncFunctionDef)


SPEC_FUNCTION_TOO_LONG = RuleSpec(
    rule="code-quality/function-too-long",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.WARNING,
    category="Complexity",
    help=(
        f"Function body exceeds {FUNCTION_MAX_LINES} logical lines. Extract cohesive sub-steps into "
        "named helpers so each function does one thing."
    ),
    style=True,
)

SPEC_FILE_TOO_LARGE = RuleSpec(
    rule="code-quality/file-too-large",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.WARNING,
    category="Complexity",
    help=(f"File exceeds {FILE_MAX_LINES} lines. Split it into focused modules grouped by responsibility."),
    style=True,
)

SPEC_DEEP_NESTING = RuleSpec(
    rule="code-quality/deep-nesting",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.WARNING,
    category="Complexity",
    help=(
        f"Control-flow nesting deeper than {MAX_NESTING}. Flatten with early returns, guard clauses, "
        "or extracted helpers."
    ),
)

SPEC_TOO_MANY_PARAMS = RuleSpec(
    rule="code-quality/too-many-params",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.WARNING,
    category="Complexity",
    help=(
        f"Function takes more than {MAX_PARAMS} required parameters. Group related arguments into a "
        "dataclass / options object, or supply sensible defaults."
    ),
)

rules: tuple[RuleSpec, ...] = (
    SPEC_FUNCTION_TOO_LONG,
    SPEC_FILE_TOO_LARGE,
    SPEC_DEEP_NESTING,
    SPEC_TOO_MANY_PARAMS,
)


# Token types that carry no code: structural/whitespace markers and comments. A physical line whose
# only tokens are in this set (or which has no tokens at all) is blank-or-comment, not a code line.
_NON_CODE_TOKENS: frozenset[int] = frozenset(
    {
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.COMMENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)


def _codeful_line_numbers(source: str) -> frozenset[int]:
    """Return the 1-based line numbers that carry real code (not blank, not comment-only).

    Driven by :mod:`tokenize` rather than text matching, so a ``#`` inside a string literal is part
    of a STRING token (codeful) and a real comment is a COMMENT token (non-code) — the two can never
    be confused. A blank line emits only NL; a comment-only line emits only COMMENT(+NL). Interior
    lines of a multi-line string carry no token of their own, so they are conservatively *not*
    counted as code (under-count bias, never over-count).
    """
    codeful: set[int] = set()
    readline = io.StringIO(source).readline
    for tok in tokenize.generate_tokens(readline):
        if tok.type in _NON_CODE_TOKENS:
            continue
        codeful.add(tok.start[0])
    return frozenset(codeful)


def _docstring_lineset(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Return the physical line numbers occupied by ``func``'s docstring, if any (else empty)."""
    if not func.body:
        return set()
    first = func.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        and first.end_lineno is not None
    ):
        return set(range(first.lineno, first.end_lineno + 1))
    return set()


def _logical_body_lines(func: ast.FunctionDef | ast.AsyncFunctionDef, codeful_lines: frozenset[int]) -> int:
    """Count the function's logical body lines.

    EXCLUDES the signature (we start from the first body statement, so a multi-line ``def`` header is
    never counted), the leading docstring, blank lines, and comment-only lines (the latter two are
    excluded because they are absent from ``codeful_lines``). ``async def`` is handled identically
    because both node types share the ``body`` attribute.

    Lines belonging to a *nested* ``def``/``class`` are intentionally still counted toward the
    enclosing function's length (a function that inlines a large helper is genuinely long as written,
    matching standard function-length linters). The nested scope is *also* measured independently
    when :func:`detect` walks to it, so each over-long scope is reported on its own line.
    """
    if not func.body:
        return 0
    body_start = func.body[0].lineno
    last = func.body[-1]
    body_end = last.end_lineno if last.end_lineno is not None else last.lineno
    docstring_lines = _docstring_lineset(func)
    return sum(
        1 for lineno in range(body_start, body_end + 1) if lineno in codeful_lines and lineno not in docstring_lines
    )


def _required_param_count(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count REQUIRED parameters, the principled way.

    Excludes ``self``/``cls`` (only when first positional), ``*args``/``**kwargs``, the bare ``*``/``/``
    separators (these never appear in ``args``/``kwonlyargs`` so they are excluded structurally), and
    any parameter carrying a default. Positional defaults bind to the *trailing* positional params;
    keyword-only defaults are keyed by name in ``kw_defaults`` (a ``None`` slot means "required").
    """
    a = func.args
    positional = a.posonlyargs + a.args
    # Drop a leading self/cls only when it is the very first positional parameter (method receiver).
    if positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]
    # The last len(defaults) positional params have defaults -> not required.
    num_pos_defaults = len(a.defaults)
    required_positional = positional[: len(positional) - num_pos_defaults] if num_pos_defaults else positional
    required = len(required_positional)
    # Keyword-only: required iff its kw_defaults slot is None.
    for default in a.kw_defaults:
        if default is None:
            required += 1
    return required


def _check_function_length(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    ctx: FileContext,
    codeful_lines: frozenset[int],
) -> Diagnostic | None:
    """Emit ``function-too-long`` when the logical body exceeds :data:`FUNCTION_MAX_LINES`."""
    logical = _logical_body_lines(func, codeful_lines)
    if logical > FUNCTION_MAX_LINES:
        return diagnostic_from_spec(
            SPEC_FUNCTION_TOO_LONG,
            file_path=ctx.path.as_posix(),
            line=func.lineno,
            message=f"function '{func.name}' is too long ({logical} logical lines)",
            detail=f"{logical} lines",
        )
    return None


def _check_too_many_params(func: ast.FunctionDef | ast.AsyncFunctionDef, ctx: FileContext) -> Diagnostic | None:
    """Emit ``too-many-params`` when required parameters exceed :data:`MAX_PARAMS`."""
    required = _required_param_count(func)
    if required > MAX_PARAMS:
        return diagnostic_from_spec(
            SPEC_TOO_MANY_PARAMS,
            file_path=ctx.path.as_posix(),
            line=func.lineno,
            message=f"function '{func.name}' has too many required parameters ({required})",
            detail=f"{required} params",
        )
    return None


def _is_elif(outer: ast.If) -> ast.If | None:
    """Return the chained ``elif`` ``If`` node when ``outer``'s ``orelse`` is an ``elif``, else None.

    ``elif`` desugars to ``If(orelse=[If(...)])``. A *true* ``elif`` is a sibling branch (same nesting
    level), whereas an explicit ``else:`` followed by an indented ``if`` is one level deeper. The two
    are distinguished by column: a chained ``elif`` shares the outer ``If``'s ``col_offset``; an
    ``else: <newline> if`` is indented further. Treating ``elif`` as a sibling is the guard that keeps
    a long ``if/elif/elif...`` ladder from being mistaken for deep nesting.
    """
    if len(outer.orelse) == 1 and isinstance(outer.orelse[0], ast.If):
        inner = outer.orelse[0]
        if inner.col_offset == outer.col_offset:
            return inner
    return None


def _nesting_bodies(stmt: ast.stmt) -> list[list[ast.stmt]]:
    """Return every nested suite of a control-flow statement that genuinely adds one level.

    For an ``if/elif/else`` ladder the whole chain is one construct: every branch body (the ``if``
    body, each chained ``elif`` body, and the trailing ``else``) is exactly ONE level deeper — the
    chain links between them do not stack depth. For ``try`` we include ``finally`` and every
    ``except`` handler; loops/``with`` include their ``body`` and any real ``else``.
    """
    if isinstance(stmt, ast.If):
        bodies: list[list[ast.stmt]] = []
        node: ast.If | None = stmt
        while node is not None:
            bodies.append(node.body)
            chained = _is_elif(node)
            if chained is None:  # tail of the chain: orelse (if any) is a real else block
                if node.orelse:
                    bodies.append(node.orelse)
                node = None
            else:
                node = chained
        return [b for b in bodies if b]
    bodies = []
    for attr in ("body", "orelse", "finalbody"):
        suite = getattr(stmt, attr, None)
        if suite:
            bodies.append(suite)
    for handler in getattr(stmt, "handlers", []) or []:
        if handler.body:
            bodies.append(handler.body)
    return [b for b in bodies if b]


def _deepest_nesting(body: list[ast.stmt], depth: int) -> tuple[int, ast.stmt | None]:
    """Recursively find the maximum control-flow nesting depth and its deepest offending statement.

    Only the *bodies* of control-flow statements (``if``/``for``/``while``/``with``/``try``, plus a
    real ``else``/``finally`` and each ``except``) add a level. A function whose statements are flat
    siblings therefore stays at depth 1 no matter how many there are — the deliberate guard against
    flagging long-but-flat code as deeply nested. A long ``if/elif`` ladder likewise stays flat (see
    :func:`_nesting_bodies`). A nested ``def``/``class`` resets to its own scope (measured
    independently when :func:`detect` walks to it).
    """
    best_depth = depth
    best_node: ast.stmt | None = None
    for stmt in body:
        if isinstance(stmt, (*_FUNC_TYPES, ast.ClassDef)):
            continue  # nested scopes are analysed independently, not as deeper nesting here
        if isinstance(stmt, _NESTING_STMTS):
            for child_body in _nesting_bodies(stmt):
                child_depth, child_node = _deepest_nesting(child_body, depth + 1)
                candidate_node = child_node if child_node is not None else stmt
                if child_depth > best_depth:
                    best_depth, best_node = child_depth, candidate_node
    return best_depth, best_node


def _check_deep_nesting(func: ast.FunctionDef | ast.AsyncFunctionDef, ctx: FileContext) -> Diagnostic | None:
    """Emit ``deep-nesting`` when a function's control-flow depth exceeds :data:`MAX_NESTING`."""
    depth, node = _deepest_nesting(func.body, 1)
    if depth > MAX_NESTING and node is not None:
        return diagnostic_from_spec(
            SPEC_DEEP_NESTING,
            file_path=ctx.path.as_posix(),
            line=node.lineno,
            message=f"control-flow nesting is too deep (depth {depth})",
            detail=f"depth {depth}",
        )
    return None


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Run all four complexity rules over one file context."""
    diagnostics: list[Diagnostic] = []

    # file-too-large: emit once at line 1 when the physical line count exceeds the cap.
    total_lines = len(ctx.lines)
    if total_lines > FILE_MAX_LINES:
        diagnostics.append(
            diagnostic_from_spec(
                SPEC_FILE_TOO_LARGE,
                file_path=ctx.path.as_posix(),
                line=1,
                message=f"file is too large ({total_lines} lines)",
                detail=f"{total_lines} lines",
            )
        )

    # Per-function rules: every function/method anywhere in the tree, each scope analysed independently.
    codeful_lines = _codeful_line_numbers(ctx.source)
    for node in ast.walk(ctx.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for finding in (
            _check_function_length(node, ctx, codeful_lines),
            _check_too_many_params(node, ctx),
            _check_deep_nesting(node, ctx),
        ):
            if finding is not None:
                diagnostics.append(finding)

    return diagnostics
