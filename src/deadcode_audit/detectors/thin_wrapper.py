"""AI-slop detector: the verbatim pass-through wrapper.

A *thin wrapper* is a function whose entire body is a single ``return f(<args>)`` (or
``return await f(<args>)``) that forwards its OWN parameters to another callable **unchanged** —
positional params in the same order, ``*args`` as ``*args``, ``**kwargs`` as ``**kwargs`` — with
no transformation, no extra/literal arguments, no reordering or renaming. Such a function adds a
name and an indirection but no behaviour; agents emit them prolifically ("just wrap the real
function"). Collapsing them removes a layer of misdirection.

The rule encodes a *structural principle* (arguments forwarded in identical roles, nothing else),
not a snippet. It is deliberately false-negative biased: anything that even might transform an
argument, add an argument, drop one, reorder them, or do more than the single ``return`` is left
alone. Recursion (the wrapper calling itself) is excluded — that is never a no-op forward.
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

THIN_WRAPPER = RuleSpec(
    rule="ai-slop/thin-wrapper",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.WARNING,
    category="Indirection",
    help=(
        "This function's whole body forwards its own parameters unchanged to another callable, "
        "adding a layer of indirection with no behaviour. Call the wrapped callable directly, or "
        "give the wrapper a reason to exist: inject a default the callee lacks, transform an "
        "argument, or validate input."
    ),
    style=True,
    fixable=False,
)

rules: tuple[RuleSpec, ...] = (THIN_WRAPPER,)

_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


def _imported_names(tree: ast.Module) -> frozenset[str]:
    """Names bound by an ``import`` in this module (the local handle a re-export alias forwards to)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return frozenset(names)


def _is_legitimate_indirection(node: _FuncDef, call: ast.Call, imported_names: frozenset[str]) -> bool:
    """True when a verbatim forward has a real reason to exist (not pointless misdirection).

    Four principled exemptions (each structural, not a name list):
      * **decorated** — a decorator registers/adapts the function (``@server.tool``, ``@router.get``,
        ``@property``); the wrapper is a framework surface, not gratuitous indirection;
      * **factory** — forwards to a CapWords callable (a class constructor by PEP-8 convention):
        ``create_x(...) -> XNode(...)`` is a factory, the indirection is the construction site;
      * **private-state accessor** — forwards to ``_GLOBAL.method(...)`` on a module-private object
        (a ContextVar / registry getter): a public accessor encapsulating private module state;
      * **re-export alias** — forwards to an imported name: a layering / stable-name re-export.
    """
    if node.decorator_list:
        return True
    repr_ = _callable_repr(call.func)
    if repr_ == "the wrapped callable":
        return False
    parts = repr_.split(".")
    root, final = parts[0], parts[-1]
    if final[:1].isupper():  # factory: target is a class constructor (CapWords)
        return True
    if root.startswith("_") and root not in ("self", "cls") and len(parts) >= 2:  # _GLOBAL.method accessor
        return True
    return root in imported_names  # re-export / aliasing of an imported callable


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Flag functions whose entire body is a verbatim, *purpose-free* pass-through of their parameters."""
    imported_names = _imported_names(ctx.tree)
    findings: list[Diagnostic] = []
    for node in ast.walk(ctx.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        call = _sole_return_call(node)
        if call is None:
            continue
        if not _is_verbatim_passthrough(node, call):
            continue
        if _is_legitimate_indirection(node, call, imported_names):
            continue  # decorated / factory / private-state accessor / re-export alias — has a reason
        target = _callable_repr(call.func)
        findings.append(
            diagnostic_from_spec(
                THIN_WRAPPER,
                file_path=ctx.path.as_posix(),
                line=node.lineno,
                message=f"'{node.name}' forwards its parameters verbatim to {target}() — a pass-through wrapper",
                detail="Every argument is one of this function's own parameters in the same role, with no change.",
            )
        )
    return findings


def _sole_return_call(node: _FuncDef) -> ast.Call | None:
    """Return the wrapped ``ast.Call`` iff the body is exactly one ``return f(...)``/``return await f(...)``.

    An optional leading bare docstring is permitted (documentation, not logic); anything else in the
    body — a second statement, a return of a non-call, ``return`` with no value — disqualifies it.
    """
    body = _strip_leading_docstring(node.body)
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    value = body[0].value
    if value is None:
        return None
    # Allow exactly one optional ``await`` layer around the call (the "return await f(...)" form).
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return None
    return value


def _strip_leading_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Drop a single leading string-literal expression statement (a docstring), if present."""
    if body and isinstance(body[0], ast.Expr) and _is_str_constant(body[0].value):
        return body[1:]
    return body


def _is_str_constant(node: ast.expr) -> bool:
    """True for a bare ``str`` literal expression (a docstring)."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_verbatim_passthrough(node: _FuncDef, call: ast.Call) -> bool:
    """True iff every argument of ``call`` is one of ``node``'s own parameters in the identical role.

    Conservative on every axis: positional params must be forwarded positionally in declaration
    order; ``*args`` forwarded as a single ``*args`` star-arg; ``**kwargs`` forwarded as a single
    ``**kwargs`` double-star; no keyword arguments, no literals, no transformations, no extra or
    missing parameters, and the call must not be recursion into the wrapper itself. Parameter
    defaults neither spare nor flag by themselves: a defaulted parameter forwarded unchanged
    injects nothing (still a verbatim pass-through), while a wrapper that *injects* its default
    omits the parameter from the call and is already spared by the drop-a-parameter check.
    """
    if _is_recursive(node, call):
        return False
    # Any keyword in the call (``f(x, mode=1)``, ``f(a=a)``, ``f(**other)`` mixed) is not a plain
    # positional pass-through; the only "keyword-ish" form we accept is the lone ``**kwargs`` star
    # which AST models as a keyword with ``arg is None`` — handled below, so reject the rest here.
    explicit_kwargs = [kw for kw in call.keywords if kw.arg is not None]
    if explicit_kwargs:
        return False

    params = node.args
    if params.kwonlyargs or params.posonlyargs:
        # pos-only / kw-only parameters can't be forwarded positionally in a way we can verify as
        # role-preserving; bail conservatively rather than risk a false positive.
        return False

    expected_positional = [a.arg for a in params.args]
    # ``def m(self, x): return self.impl(x)`` is a verbatim method forward: the first parameter is
    # consumed unchanged as the receiver of the wrapped attribute, so it is not (and must not be)
    # repeated in the forwarded argument list. Drop it from the expectations when, and only when,
    # the call target is ``<first-param>.<attr>``.
    if _receiver_is_first_param(call.func, expected_positional):
        expected_positional = expected_positional[1:]

    forwarded_positional, vararg_ok = _split_call_args(call)
    if forwarded_positional is None:
        return False  # an arg was not a bare parameter name, or a star-arg appeared mid-list

    if forwarded_positional != expected_positional:
        return False

    # ``*args`` must be forwarded iff it is declared, and as the matching name.
    if not _star_matches(params.vararg, vararg_ok):
        return False

    # ``**kwargs`` must be forwarded iff it is declared, and as the matching name.
    return _double_star_matches(params.kwarg, call.keywords)


def _split_call_args(call: ast.Call) -> tuple[list[str] | None, str | None]:
    """Split ``call.args`` into (forwarded positional param names, trailing ``*name`` name).

    Returns ``(None, None)`` if any positional argument is not a bare ``Name`` (i.e. it is a
    literal, attribute, transformed expression, or nested call), or if a ``*expr`` star-arg is not
    a bare name or is not last. The trailing star name is ``None`` when no ``*args`` is forwarded.
    """
    names: list[str] = []
    star_name: str | None = None
    for index, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            # A ``*expr`` is only acceptable as the final positional and only if it forwards a name.
            if index != len(call.args) - 1 or not isinstance(arg.value, ast.Name):
                return None, None
            star_name = arg.value.id
            continue
        if not isinstance(arg, ast.Name):
            return (
                None,
                None,
            )  # literal / attribute / call / binop / etc. — a transform, not a forward
        names.append(arg.id)
    return names, star_name


def _star_matches(vararg: ast.arg | None, forwarded_star: str | None) -> bool:
    """``*args`` is verbatim iff declared exactly when forwarded and under the same name."""
    if vararg is None:
        return forwarded_star is None
    return forwarded_star == vararg.arg


def _double_star_matches(kwarg: ast.arg | None, keywords: list[ast.keyword]) -> bool:
    """``**kwargs`` is verbatim iff declared exactly when a single ``**name`` forwards the same name."""
    double_stars = [kw for kw in keywords if kw.arg is None]
    if kwarg is None:
        return not double_stars
    if len(double_stars) != 1:
        return False
    value = double_stars[0].value
    return isinstance(value, ast.Name) and value.id == kwarg.arg


def _receiver_is_first_param(func: ast.expr, expected_positional: list[str]) -> bool:
    """True for ``<first-param>.<attr>`` — the wrapped call is a method on the first parameter.

    Only a single attribute hop on a bare ``Name`` receiver counts (``self.impl`` /
    ``obj.handler``); deeper chains like ``self.a.b`` are not treated as a verbatim receiver and
    fall through to the strict positional check (which will then not match).
    """
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and bool(expected_positional)
        and func.value.id == expected_positional[0]
    )


def _is_recursive(node: _FuncDef, call: ast.Call) -> bool:
    """True when the call targets the wrapper itself — never a no-op forward.

    Both self-call shapes count: a bare-name recursion (``def f(): return f(...)``) and method
    self-recursion through the receiver (``def f(self, x): return self.f(x)``), where the receiver
    is the wrapper's own first positional parameter.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == node.name
    if isinstance(func, ast.Attribute) and func.attr == node.name:
        positional = node.args.posonlyargs + node.args.args
        return bool(positional) and isinstance(func.value, ast.Name) and func.value.id == positional[0].arg
    return False


def _callable_repr(func: ast.expr) -> str:
    """Dotted name of the wrapped callable for the message (``self.method``, ``mod.fn``)."""
    parts: list[str] = []
    current: ast.expr | None = func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    elif current is not None:
        return "the wrapped callable"
    return ".".join(reversed(parts)) if parts else "the wrapped callable"
