"""AI-slop Python-idiom detectors: non-Pythonic shapes an LLM emits by reflex.

Four structural anti-idioms, each encoding a *principle* (not a snippet) with conservative,
rename-proof guards so legitimate near-misses do not fire:

* ``python-range-len-loop``    — ``for t in range(len(x)):`` (manual index where ``enumerate`` /
  direct iteration is meant). A ``range`` with a start/step (>1 arg) is intentional and skipped.
* ``python-chained-dict-get``  — ``d.get(x).get(y)`` where the inner ``.get`` has NO default: a
  miss yields ``None`` and ``None.get(y)`` raises ``AttributeError``. An explicit default (incl.
  the idiomatic ``d.get(x, {}).get(y)``) is deliberate, crash-safe defensive navigation and is fine.
* ``python-repetitive-dispatch`` — an ``if/elif`` chain (>=4 branches) each comparing the SAME
  left-hand expression by ``==``/``in`` to a constant — a dict dispatch table written out longhand.
* ``python-isinstance-ladder``   — an ``if/elif`` chain (>=4 branches) each an
  ``isinstance(<same x>, T)`` test — a handler map / normalised representation written longhand.

All four are AST-driven, so they are immune to text inside strings/comments and need no masking.
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

# Minimum branch count for a chain to read as a longhand table rather than ordinary control flow.
# Below this, an if/elif chain is normal, readable code — flagging it would cry wolf.
_MIN_CHAIN_BRANCHES = 4

RANGE_LEN_SPEC = RuleSpec(
    rule="ai-slop/python-range-len-loop",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.INFO,
    category="Python idiom",
    help=(
        "Iterating `range(len(x))` to index back into `x` is a non-Pythonic manual index. "
        "Use `enumerate(x)` when you need the index, or iterate `x` directly when you do not."
    ),
    style=True,
    fixable=False,
)

CHAINED_GET_SPEC = RuleSpec(
    rule="ai-slop/python-chained-dict-get",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.WARNING,
    category="Python idiom",
    help=(
        "Chaining `.get(x).get(y)` with NO intermediate default crashes (AttributeError) when `x` "
        "is missing — the inner get returns None. Give the inner get a default (e.g. `{}`) or "
        "validate it explicitly. An explicit default like `.get(x, {}).get(y)` is already safe."
    ),
    fixable=False,
)

REPETITIVE_DISPATCH_SPEC = RuleSpec(
    rule="ai-slop/python-repetitive-dispatch",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.WARNING,
    category="Python idiom",
    help=(
        "A long if/elif chain that compares the same value to a series of constants is a dispatch "
        "table written longhand. Replace it with a dict mapping each constant to its handler/result."
    ),
    fixable=False,
)

ISINSTANCE_LADDER_SPEC = RuleSpec(
    rule="ai-slop/python-isinstance-ladder",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.WARNING,
    category="Python idiom",
    help=(
        "A long if/elif chain of isinstance checks on the same value is a type-dispatch ladder. "
        "Use a {type: handler} map, single-dispatch, or a normalised representation instead."
    ),
    fixable=False,
)

rules: tuple[RuleSpec, ...] = (
    RANGE_LEN_SPEC,
    CHAINED_GET_SPEC,
    REPETITIVE_DISPATCH_SPEC,
    ISINSTANCE_LADDER_SPEC,
)


def _is_bare_name_call(node: ast.expr, name: str) -> bool:
    """True for a call to a bare global ``name(...)`` — e.g. ``len(...)``, ``range(...)``.

    Requires ``node.func`` to be a plain ``Name`` so that an attribute call such as
    ``obj.range(...)`` or ``np.len(...)`` (a different, possibly meaningful API) is never matched.
    """
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _detect_range_len(tree: ast.Module, path_posix: str) -> list[Diagnostic]:
    """Flag ``for <t> in range(len(<expr>)):`` — manual index where iteration is meant.

    Guard: ``range`` is only the idiom when called with a SINGLE positional argument that is itself
    a ``len(...)`` call. ``range(start, len(x))`` / ``range(0, len(x), 2)`` (>1 arg) express an
    intentional start/step and are left alone, as is any ``range`` whose sole arg is not ``len(...)``.
    """
    findings: list[Diagnostic] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        it = node.iter
        if not _is_bare_name_call(it, "range"):
            continue
        assert isinstance(it, ast.Call)  # _is_bare_name_call guarantees a Call node
        # range(...) must take exactly one positional arg (no start/step) and no keywords.
        if len(it.args) != 1 or it.keywords:
            continue
        sole_arg = it.args[0]
        if _is_bare_name_call(sole_arg, "len"):
            findings.append(
                diagnostic_from_spec(
                    RANGE_LEN_SPEC,
                    file_path=path_posix,
                    line=node.lineno,
                    message="`for ... in range(len(...))` indexes manually; use enumerate() or iterate directly",
                )
            )
    return findings


def _get_call_default(node: ast.Call) -> tuple[bool, ast.expr | None]:
    """Classify a ``<recv>.get(...)`` call: ``(is_get_call, default_expr_or_None)``.

    ``is_get_call`` is True only for an attribute call named ``get`` with 1 or 2 positional args
    and no keywords (``dict.get`` takes no keyword args; a 2-arg form carries an explicit default).
    The default expr is the second positional arg when present, else ``None`` (meaning the
    implicit ``None`` default, which — like ``{}`` — produces a value you cannot re-``.get`` safely).
    """
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
        return (False, None)
    if node.keywords or not (1 <= len(node.args) <= 2):
        return (False, None)
    default = node.args[1] if len(node.args) == 2 else None
    return (True, default)


def _detect_chained_get(tree: ast.Module, path_posix: str) -> list[Diagnostic]:
    """Flag ``X.get(...).get(...)`` where the INNER (receiver) get has NO default (crash risk).

    The outer node is ``<recv>.get(...)`` and its receiver ``<recv>`` is itself a ``.get(...)``
    call. Flagged ONLY when the inner get supplies no default (implicit ``None``): a lookup miss
    then yields ``None`` and the outer ``None.get(...)`` raises ``AttributeError`` at runtime.

    An explicit intermediate default — most commonly ``{}`` (``d.get(x, {}).get(y)``) — is a
    deliberate, safe defensive-navigation idiom (a miss yields ``None``, never a crash), so it is
    NOT flagged: that is intentional default-on-absent, not hidden data. (Reviewed call: the prior
    behaviour flagged ``{}`` too, which was a false positive on this codebase's idiomatic chaining.)

    Guards (each rename-proof, structural):
      * a single ``.get(...)`` never fires (the receiver must itself be a get call);
      * any explicit second-arg default (``{}`` or otherwise) means a deliberate, crash-safe chain;
      * keyword-bearing or >2-arg ``.get`` look-alikes are not treated as ``dict.get`` at all;
      * a 3+ chain is reported ONCE, at its outermost crashing pair — the inner pairs describe the
        same crash, so emitting one finding per pair was pure duplication.
    """
    findings: list[Diagnostic] = []
    reported_chain: set[int] = set()  # ids of inner get-calls already covered by an emitted finding
    for node in ast.walk(tree):  # BFS: outer calls are visited before the calls nested in them
        if not isinstance(node, ast.Call):
            continue
        outer_is_get, _ = _get_call_default(node)
        if not outer_is_get:
            continue
        # _get_call_default guaranteed node.func is an Attribute named "get"; its value is the receiver.
        assert isinstance(node.func, ast.Attribute)
        receiver = node.func.value
        if not isinstance(receiver, ast.Call):
            continue
        inner_is_get, inner_default = _get_call_default(receiver)
        if not inner_is_get:
            continue
        if id(node) in reported_chain:
            continue  # an outer get of this same chain already reported the crash risk
        # Fire ONLY when the intermediate get has no default: a miss yields None and the outer
        # `None.get(...)` raises AttributeError. An explicit default (incl. `{}`) is a deliberate,
        # crash-safe defensive chain and is intentionally spared.
        if inner_default is None:
            findings.append(
                diagnostic_from_spec(
                    CHAINED_GET_SPEC,
                    file_path=path_posix,
                    line=node.lineno,
                    message="chained `.get(...).get(...)` with no intermediate default risks AttributeError on a miss",
                )
            )
            # Mark every get-call below this one so longer chains do not re-report the same crash.
            chain: ast.expr = receiver
            while isinstance(chain, ast.Call):
                reported_chain.add(id(chain))
                nested = chain.func.value if isinstance(chain.func, ast.Attribute) else None
                if not isinstance(nested, ast.Call):
                    break
                chain = nested
    return findings


def _iter_if_chain(node: ast.If) -> list[ast.If]:
    """Return every ``If`` node of one ``if/elif`` chain, top-down (the head plus each ``elif``).

    An ``elif`` is encoded as a lone ``If`` inside the parent's ``orelse``; a trailing bare
    ``else`` is a non-``If`` ``orelse`` body and is not part of the returned branch list.
    """
    chain = [node]
    current = node
    while len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
        current = current.orelse[0]
        chain.append(current)
    return chain


def _is_elif_continuation(node: ast.If, parents: dict[int, ast.AST]) -> bool:
    """True when ``node`` is the ``elif`` body of another ``If`` (so it is not a chain head)."""
    parent = parents.get(id(node))
    return isinstance(parent, ast.If) and len(parent.orelse) == 1 and parent.orelse[0] is node


def _build_parent_map(tree: ast.Module) -> dict[int, ast.AST]:
    """Map ``id(child) -> parent`` for every node, so a chain head can be told from an ``elif``."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _equality_dispatch_key(test: ast.expr) -> tuple[str, str] | None:
    """Return a stable ``(lhs_dump, op)`` key for a single ``lhs == const`` / ``lhs in const`` test.

    Returns ``None`` unless the test is exactly one comparison, against a single constant or
    constant container, with a ``==`` or ``in`` operator. Compound (``and``/``or``), negated, or
    chained (``a == b == c``) tests yield ``None`` so the branch breaks the dispatch run — a
    real dispatch table has one clean equality per branch.

    The lhs is keyed by ``ast.dump`` (ignoring positions) so two branches testing the *same*
    expression match regardless of formatting, while a *different* lhs gives a different key.
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return None
    op = test.ops[0]
    if isinstance(op, ast.Eq):
        op_name = "=="
    elif isinstance(op, ast.In):
        op_name = "in"
    else:
        return None
    comparator = test.comparators[0]
    if not _is_constant_operand(comparator):
        return None
    return (ast.dump(test.left), op_name)


def _is_constant_operand(node: ast.expr) -> bool:
    """True for a literal constant or a literal container of constants (a dispatch *case* value).

    Covers ``"x"`` / ``3`` / ``None`` and ``("a", "b")`` / ``["a", "b"]`` / ``{"a", "b"}`` whose
    every element is itself constant — the right-hand side of a `== const` / `in {consts}` case.
    A non-literal comparator (a variable, attribute, call) means the branch is not a static case
    and must not extend a dispatch run.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(isinstance(elt, ast.Constant) for elt in node.elts)
    return False


def _isinstance_subject_key(test: ast.expr) -> str | None:
    """Return the ``ast.dump`` of the FIRST arg of a sole ``isinstance(x, T)`` test, else ``None``.

    Returns ``None`` for anything that is not exactly a bare ``isinstance(...)`` call with two
    positional args and no keywords (compound/negated tests, or a different predicate, break the
    run). The subject is keyed by structure so the *same* ``x`` matches across branches and a
    *different* ``x`` does not.
    """
    if not _is_bare_name_call(test, "isinstance"):
        return None
    assert isinstance(test, ast.Call)  # _is_bare_name_call guarantees a Call node
    if len(test.args) != 2 or test.keywords:
        return None
    return ast.dump(test.args[0])


def _detect_chains(tree: ast.Module, path_posix: str) -> list[Diagnostic]:
    """Flag long ``if/elif`` chains that are dispatch tables or isinstance ladders in disguise.

    A chain qualifies only when it has ``>=_MIN_CHAIN_BRANCHES`` branches AND *every* branch
    shares the discriminator:

      * repetitive-dispatch: every branch is ``<same lhs> ==/in <const>`` (same lhs dump, same op);
      * isinstance-ladder: every branch is ``isinstance(<same first arg>, T)``.

    Any branch that is compound, negated, tests a different subject, or is not the expected single
    predicate resets the run to ``None`` — so a mixed chain never reaches the threshold and a
    trailing plain ``else`` (not part of the branch list) is irrelevant. Only chain *heads* are
    examined (``elif`` continuations are skipped) to report once, at the opening ``if``.
    """
    findings: list[Diagnostic] = []
    parents = _build_parent_map(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or _is_elif_continuation(node, parents):
            continue
        branches = _iter_if_chain(node)
        if len(branches) < _MIN_CHAIN_BRANCHES:
            continue
        findings.extend(_classify_chain(branches, node, path_posix))
    return findings


def _classify_chain(branches: list[ast.If], head: ast.If, path_posix: str) -> list[Diagnostic]:
    """Emit at most one finding for a long chain whose branches are *uniformly* one anti-idiom."""
    eq_keys = [_equality_dispatch_key(branch.test) for branch in branches]
    if all(key is not None for key in eq_keys) and len(set(eq_keys)) == 1:
        return [
            diagnostic_from_spec(
                REPETITIVE_DISPATCH_SPEC,
                file_path=path_posix,
                line=head.lineno,
                message=(
                    f"{len(branches)}-branch if/elif compares the same value to constants; " "use a dict dispatch table"
                ),
            )
        ]
    isinstance_keys = [_isinstance_subject_key(branch.test) for branch in branches]
    if all(key is not None for key in isinstance_keys) and len(set(isinstance_keys)) == 1:
        return [
            diagnostic_from_spec(
                ISINSTANCE_LADDER_SPEC,
                file_path=path_posix,
                line=head.lineno,
                message=(
                    f"{len(branches)}-branch isinstance() ladder on the same value; "
                    "use a {type: handler} map or single-dispatch"
                ),
            )
        ]
    return []


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Run all four Python-idiom detectors over one file context."""
    path_posix = ctx.path.as_posix()
    findings: list[Diagnostic] = []
    findings.extend(_detect_range_len(ctx.tree, path_posix))
    findings.extend(_detect_chained_get(ctx.tree, path_posix))
    findings.extend(_detect_chains(ctx.tree, path_posix))
    return findings
