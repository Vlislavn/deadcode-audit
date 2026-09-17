"""AI-slop exception-handler detectors (ported from aislop's swallowed-exception family).

Two structural anti-patterns that weak coding agents leave behind around ``try/except``:

* **swallowed-exception** (ERROR) — a handler that neither handles nor propagates the error:
  a sole ``pass``, a sole bare ``...``, a sole ``continue``/``break`` (error dropped, control
  flow just moves on), or a sole *log-and-continue* (``logger.*`` / ``logging.*`` / ``log.*`` /
  ``print`` / ``warnings.warn``). This is exactly the failure mode the source monorepo's own fail-fast rule
  (SOURCE-D001) forbids: an error is silently dropped, so a later step runs on a broken state.

* **redundant-try-catch** (WARNING) — a handler whose entire body is a bare ``raise`` (re-raise
  that adds nothing): the ``try/except`` is pure ceremony and can be deleted.

Both rules are **AST-only and exact-body** matchers, deliberately false-negative-biased. They
fire only when the handler body is EXACTLY the slop shape — any extra statement, any ``raise``
(re-raise/wrap), any assignment/return/other call means real handling is happening and we stay
silent. None of the matched shapes depends on a variable name, so the guards cannot be defeated
by renaming.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from deadcode_audit.diagnostic import (
    ENGINE_AI_SLOP,
    Diagnostic,
    RuleSpec,
    Severity,
    diagnostic_from_spec,
)

if TYPE_CHECKING:
    from deadcode_audit.framework import FileContext

SWALLOWED = RuleSpec(
    rule="ai-slop/swallowed-exception",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.ERROR,
    category="Error handling",
    help=(
        "This except handler swallows the error (bare 'pass' or log-and-continue): execution "
        "falls through on a broken state. Either handle it concretely (recover, set a fallback "
        "value, return) or re-raise — do not silently drop it (SOURCE-D001 fail-fast)."
    ),
)

REDUNDANT = RuleSpec(
    rule="ai-slop/redundant-try-catch",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.WARNING,
    category="Error handling",
    help=(
        "This except handler's whole body is a bare 're-raise that adds nothing — delete the "
        "try/except and let the error propagate unchanged. If you mean to add context, use "
        "'raise NewError(...) from exc'; if you mean to clean up, use try/finally."
    ),
    style=True,
)

rules: tuple[RuleSpec, ...] = (SWALLOWED, REDUNDANT)

# Bare callables and dotted-attribute roots that count as a pure log/print no-op. A call is a
# "log-and-continue" when it is EITHER one of the bare names, OR an attribute call whose leftmost
# Name is one of the logging roots (covering ``logger.error(...)``, ``logging.warning(...)``,
# ``log.debug(...)``, ``self.logger.exception(...)`` is intentionally NOT matched — see below).
_LOG_BARE_CALLS = frozenset({"print"})
_LOG_ROOT_NAMES = frozenset({"logger", "logging", "log"})
# Fully-qualified call shapes that are still pure logging even though they are not a *-root chain.
_LOG_DOTTED_CALLS = frozenset({("warnings", "warn")})


def _attribute_root_name(node: ast.expr) -> str | None:
    """Return the leftmost ``Name`` id of an attribute/call chain (``a.b().c`` -> ``a``), else None.

    Call boundaries are traversed so ``logging.getLogger(__name__).warning(...)`` — the standard
    module-logger idiom — roots at ``logging`` and is still recognised as a pure log call.
    """
    current = node
    while isinstance(current, (ast.Attribute, ast.Call)):
        current = current.value if isinstance(current, ast.Attribute) else current.func
    if isinstance(current, ast.Name):
        return current.id
    return None


def _dotted_call_parts(func: ast.Attribute) -> tuple[str, str] | None:
    """Return ``(root_name, attr)`` for a one-level ``root.attr`` call, else None."""
    if isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return None


def _is_log_or_print_call(call: ast.Call) -> bool:
    """True iff ``call`` is a pure logging/print no-op (the only kind of 'handling' we treat as slop).

    Matches (conservatively):
      * a bare ``print(...)``;
      * a ``warnings.warn(...)`` call;
      * any attribute call whose attribute chain is ROOTED at a bare ``logger`` / ``logging`` /
        ``log`` name (``logger.error(...)``, ``logging.getLogger(__name__).warning(...)`` would
        root at ``logging`` and match — both are still log-only no-ops).

    Anything else (e.g. ``self.handle(...)``, ``cache.set(...)``, ``foo()``) is NOT a log call,
    so the handler is treated as doing real work and is left alone.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _LOG_BARE_CALLS
    if isinstance(func, ast.Attribute):
        parts = _dotted_call_parts(func)
        if parts is not None and parts in _LOG_DOTTED_CALLS:
            return True
        root = _attribute_root_name(func)
        return root in _LOG_ROOT_NAMES
    return False


def _body_contains_raise(body: list[ast.stmt]) -> bool:
    """True if any ``raise`` statement appears anywhere in the handler body (incl. nested)."""
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Raise):
                return True
    return False


def _is_swallow_body(body: list[ast.stmt]) -> bool:
    """True iff the handler body is EXACTLY a swallow — one statement that neither handles nor
    propagates: ``pass``, a bare ``...``, a ``continue``/``break`` that just moves control flow
    past the error, or a sole log/print call.

    Exactness is the guard: a body of length != 1, or a single statement that is anything other
    than those shapes, means real handling and is not flagged.
    """
    if len(body) != 1:
        return False
    statement = body[0]
    if isinstance(statement, (ast.Pass, ast.Continue, ast.Break)):
        return True
    if isinstance(statement, ast.Expr):
        value = statement.value
        if isinstance(value, ast.Constant) and value.value is Ellipsis:
            return True  # ``except E: ...`` — same placeholder as ``pass``
        if isinstance(value, ast.Call):
            return _is_log_or_print_call(value)
    return False


def _swallow_shape(statement: ast.stmt) -> str:
    """Human label for the sole swallow statement (for the finding message)."""
    if isinstance(statement, ast.Pass):
        return "pass"
    if isinstance(statement, ast.Continue):
        return "continue"
    if isinstance(statement, ast.Break):
        return "break"
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        return "log-and-continue"
    return "..."


def _is_bare_reraise_body(body: list[ast.stmt]) -> bool:
    """True iff the handler body is EXACTLY a single bare ``raise`` (no exception, no ``from``).

    ``raise NewError(...) [from exc]`` carries ``exc`` and adds context -> NOT this shape. A bare
    ``raise`` preceded by any cleanup statement has length > 1 -> NOT this shape either.
    """
    if len(body) != 1:
        return False
    statement = body[0]
    return isinstance(statement, ast.Raise) and statement.exc is None and statement.cause is None


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Flag swallowed-exception and redundant bare-reraise handlers in one file (AST-only)."""
    diagnostics: list[Diagnostic] = []
    file_path = ctx.path.as_posix()
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = node.body
        # A re-raise anywhere in the body means the error IS propagated -> never a swallow.
        if _body_contains_raise(body):
            # The only re-raise shape that is itself slop is the *sole bare* re-raise.
            if _is_bare_reraise_body(body):
                diagnostics.append(
                    diagnostic_from_spec(
                        REDUNDANT,
                        file_path=file_path,
                        line=node.lineno,
                        message="except handler only re-raises with a bare 'raise' — the try/except adds nothing",
                    )
                )
            continue
        if _is_swallow_body(body):
            shape = _swallow_shape(body[0])
            diagnostics.append(
                diagnostic_from_spec(
                    SWALLOWED,
                    file_path=file_path,
                    line=node.lineno,
                    message=f"except handler swallows the error ({shape}) without handling or re-raising it",
                )
            )
    return diagnostics
