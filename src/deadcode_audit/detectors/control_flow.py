"""Control-flow AI-slop detectors: constant branch conditions and empty function bodies.

Two structural smells that LLM-generated code leaves behind:

* ``ai-slop/constant-condition`` — an ``if``/``elif`` whose test is a *literal constant*
  (``True``/``False``/``0``/``1``/``None`` or a string literal). A branch on a constant is
  dead by construction: the author hardcoded a value where a runtime expression belonged
  (a stubbed feature flag, a leftover ``if True:`` scaffold). The idiomatic ``while True:``
  loop is explicitly out of scope, and a *named* condition (``if TYPE_CHECKING:``,
  ``if DEBUG:``) is a legitimate compile/config switch, not a constant — so only ``ast.Constant``
  tests fire.

* ``ai-slop/empty-function`` — a function whose body is *exactly* ``pass`` or ``...``
  (optionally a docstring first). When it is not a declared stub (abstract method, overload,
  Protocol/ABC member) this is an unfinished placeholder. The guards are recognised stub
  contexts; everything else with a real body is silent.

Both rules are AST-based (immune to string/comment self-matching); no text scanning is needed.
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

CONSTANT_CONDITION = RuleSpec(
    rule="ai-slop/constant-condition",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.WARNING,
    category="Control flow",
    help=(
        "This branch tests a literal constant, so it is dead by construction: one side never "
        "runs. Replace the hardcoded value with the real runtime condition, or delete the dead "
        "branch. (Named flags like TYPE_CHECKING/DEBUG and `while True:` are intentionally fine.)"
    ),
    style=False,
    fixable=False,
)

EMPTY_FUNCTION = RuleSpec(
    rule="ai-slop/empty-function",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.INFO,
    category="Control flow",
    help=(
        "This function body is only `pass`/`...` (a placeholder). Implement it, or — if it is a "
        "real stub — mark it (@abstractmethod/@overload) or declare it on a Protocol/ABC so the "
        "intent is explicit."
    ),
    style=False,
    fixable=False,
)

rules: tuple[RuleSpec, ...] = (CONSTANT_CONDITION, EMPTY_FUNCTION)

# --- ai-slop/constant-condition -------------------------------------------------------------

# Names whose *value* is a literal we treat as a constant branch test. A bare-string test is
# also a constant, but we do not enumerate strings here — we type-check the Constant value.
_CONSTANT_BRANCH_VALUES = (True, False, None)


def _is_constant_branch_test(test: ast.expr) -> bool:
    """True when an ``if``/``elif`` test is a literal constant (never a Name, call, or compare).

    Only a bare ``ast.Constant`` qualifies. ``True``/``False``/``None``, any ``int``/``float``
    literal, and any ``str``/``bytes`` literal are constants. A ``Name`` (``TYPE_CHECKING``,
    ``DEBUG``), a comparison, a call, an attribute, or any compound expression is NOT a constant
    and is left alone — that is the central false-positive guard.
    """
    if not isinstance(test, ast.Constant):
        return False
    value = test.value
    # ``bool`` is an ``int`` subclass; check it first so True/False are handled explicitly.
    if isinstance(value, bool):
        return value in _CONSTANT_BRANCH_VALUES
    if value is None:
        return True
    return isinstance(value, (int, float, complex, str, bytes))


def _describe_constant(test: ast.Constant) -> str:
    """Short human label for the literal in the message (no raw long string contents)."""
    value = test.value
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, complex)):
        return repr(value)
    if isinstance(value, str):
        return "an empty string literal" if value == "" else "a non-empty string literal"
    if isinstance(value, bytes):
        return "a bytes literal"
    return "a literal constant"


def _detect_constant_conditions(ctx: FileContext) -> list[Diagnostic]:
    """Flag every ``if``/``elif`` whose test is a literal constant.

    ``while`` is never visited, so ``while True:`` is structurally out of scope. ``elif`` is an
    ``ast.If`` nested in ``orelse``; ``ast.walk`` reaches it, so we report each independently.
    Ternary ``IfExp`` is a different node type and is never matched (out of scope by design).
    """
    findings: list[Diagnostic] = []
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.If):
            continue
        if not _is_constant_branch_test(node.test):
            continue
        assert isinstance(node.test, ast.Constant)  # narrowed by _is_constant_branch_test
        label = _describe_constant(node.test)
        findings.append(
            diagnostic_from_spec(
                CONSTANT_CONDITION,
                file_path=ctx.path.as_posix(),
                line=node.lineno,
                message=f"branch condition is {label}; one side of this `if` can never run",
            )
        )
    return findings


# --- ai-slop/empty-function -----------------------------------------------------------------

# Decorator (simple-name or dotted-tail) markers that declare a function is an intentional stub.
_STUB_DECORATOR_NAMES = frozenset({"abstractmethod", "overload", "abstractproperty"})


def _decorator_tail_name(decorator: ast.expr) -> str | None:
    """Return the final identifier of a decorator expression (``a.b.c`` -> ``c``), else None.

    Handles bare names (``@overload``), dotted names (``@typing.overload``,
    ``@abc.abstractmethod``), and called decorators (``@functools.wraps(...)`` -> ``wraps``)
    so a parenthesised stub decorator is still recognised.
    """
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _has_stub_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when any decorator marks the function as an abstract/overload stub."""
    return any(_decorator_tail_name(dec) in _STUB_DECORATOR_NAMES for dec in func.decorator_list)


def _base_is_stub_context(base: ast.expr) -> bool:
    """True when a class base names Protocol/ABC (a stub-defining context).

    Any base whose final identifier is exactly ``ABC`` / ``ABCMeta``, or is exactly ``Protocol``
    or *ends with* ``Protocol`` (e.g. ``Renderer`` would NOT match, but ``RendererProtocol``
    would), declares a context where ``pass``/``...`` bodies are expected interface members.
    """
    tail = _decorator_tail_name(base)  # same final-identifier extraction works for base exprs
    if tail is None:
        return False
    if tail in {"ABC", "ABCMeta"}:
        return True
    return tail == "Protocol" or tail.endswith("Protocol")


def _is_empty_body(body: list[ast.stmt]) -> bool:
    """True when a body is EXACTLY ``pass`` / ``...`` (optionally one leading docstring).

    A docstring alone is NOT empty — documentation is a real body. Empty means the executable
    part is precisely a single ``pass`` or a single bare ``...`` expression. Any other statement
    (a ``return``, a ``raise NotImplementedError``, a real call) means the function has a body
    and must not be flagged.
    """
    statements = list(body)
    # Strip a single leading docstring, if present.
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    if len(statements) != 1:
        return False
    only = statements[0]
    if isinstance(only, ast.Pass):
        return True
    return isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant) and only.value.value is Ellipsis


def _enclosing_class_is_stub(stack: list[ast.AST], func: ast.AST) -> bool:
    """True when the function's immediate enclosing class derives from a Protocol/ABC base.

    ``stack`` is the chain of ancestor nodes; the nearest ``ClassDef`` ancestor (if any) is the
    method's owner. A function nested inside another function (with no class between) is NOT a
    method, so it is judged on its own merits.
    """
    for ancestor in reversed(stack):
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)) and ancestor is not func:
            return False  # a function boundary closer than any class -> not a direct method
        if isinstance(ancestor, ast.ClassDef):
            return any(_base_is_stub_context(base) for base in ancestor.bases)
    return False


def _in_type_checking_block(stack: list[ast.AST]) -> bool:
    """True when an ancestor is an ``if TYPE_CHECKING:`` block.

    Anything defined under ``if TYPE_CHECKING:`` is a type-only declaration that never executes at
    runtime — a ``...``/``pass`` body there is an intentional interface stub (e.g. a ``Protocol``-like
    shadow class used purely to type cross-module attribute access), not an unfinished placeholder.
    Matches a bare ``TYPE_CHECKING`` name and the qualified ``typing.TYPE_CHECKING`` attribute.
    """
    for ancestor in stack:
        if not isinstance(ancestor, ast.If):
            continue
        test = ancestor.test
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
            return True
    return False


def _detect_empty_functions(ctx: FileContext) -> list[Diagnostic]:
    """Flag functions whose body is only ``pass``/``...`` outside a recognised stub context.

    We carry an explicit ancestor stack so a method's enclosing class can be inspected (to honour
    the Protocol/ABC guard) without a second pass. Guards: stub decorators on the function, a
    Protocol/ABC enclosing class, and an enclosing ``if TYPE_CHECKING:`` block (type-only stubs).
    """
    findings: list[Diagnostic] = []
    _visit_for_empty(ctx, ctx.tree, [], findings)
    return findings


def _visit_for_empty(ctx: FileContext, node: ast.AST, stack: list[ast.AST], findings: list[Diagnostic]) -> None:
    """Depth-first walk that tracks the ancestor stack so methods see their enclosing class."""
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _is_empty_body(node.body)
        and not _has_stub_decorator(node)
        and not _enclosing_class_is_stub(stack, node)
        and not _in_type_checking_block(stack)
    ):
        findings.append(
            diagnostic_from_spec(
                EMPTY_FUNCTION,
                file_path=ctx.path.as_posix(),
                line=node.lineno,
                message=f"function `{node.name}` has only a `pass`/`...` body (unfinished placeholder)",
            )
        )
    stack.append(node)
    for child in ast.iter_child_nodes(node):
        _visit_for_empty(ctx, child, stack, findings)
    stack.pop()


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Run both control-flow detectors over one file context."""
    return _detect_constant_conditions(ctx) + _detect_empty_functions(ctx)
