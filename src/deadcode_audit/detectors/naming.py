"""Detector: ``ai-slop/generic-naming`` — semantically-empty, AI-generated identifier names.

LLM scaffolding loves placeholder identifiers (``foo``, ``bar``) and "stem + counter" names
(``helper_2``, ``data1``, ``result_3``). These carry no domain meaning and are a reliable tell of
machine-generated filler that a human reviewer would have named.

The rule encodes a *structural principle*, not a snippet list:

* (a) a generic stem followed by a numeric suffix — ``<stem>`` optionally + ``_`` + digits —
  where ``<stem>`` is a closed set of contentless English nouns/abbreviations. The numeric suffix
  is the load-bearing signal: a counter on a generic noun is what makes the name empty.
* (b) one of the canonical metasyntactic placeholders (``foo``/``bar``/``baz``/``qux``/``foobar``)
  used as a ``def``/``class`` name.

False-positive guards (the dominant risk is flagging legitimate code):

* Only ``def`` / ``class`` definitions and *module-level* assignment targets are considered — never
  arbitrary local variables, comprehension targets, ``for`` targets, function parameters, attribute
  or subscript targets. ``data`` as a local is normal; ``data1`` defined at module scope is not.
* A bare generic noun with **no** numeric suffix (``data``, ``result``, ``value``) is never flagged —
  only the stem+counter shape. This is what keeps real domain code (``result = compute()``) safe.
* Single-letter loop indices (``i``/``j``/``k``) and dunder names (``__all__``, ``__init__``) never
  fire — they cannot match either pattern, and dunders are excluded defensively.
* The placeholder set fires only on ``def``/``class`` names, not on assignments: a module constant
  literally named ``foo`` is unusual but the spec scopes (b) to definitions, so we stay conservative.
* Match is on the *whole* identifier (``^...$``) so ``datapoint``, ``foobar_loader``, ``result_set``,
  ``metadata`` and ``foothold`` (substring ``foo``) are not flagged.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

from deadcode_audit.diagnostic import ENGINE_AI_SLOP, Diagnostic, RuleSpec, Severity, diagnostic_from_spec

if TYPE_CHECKING:
    from deadcode_audit.framework import FileContext

# Closed set of contentless stems. Each is a generic noun/abbreviation an LLM reaches for when it
# has nothing meaningful to call something. The *numeric suffix* is mandatory for these (pattern a):
# a counter on a generic noun is the empty-name tell.
_GENERIC_STEMS = (
    "helper",
    "data",
    "temp",
    "tmp",
    "result",
    "value",
    "val",
    "item",
    "obj",
    "var",
    "arg",
    "param",
    "func",
    "cls",
    "thing",
    "stuff",
    "test",
)

# Pattern (a): <stem> + optional '_' + one-or-more digits, whole-identifier, case-insensitive.
_STEM_COUNTER_RE = re.compile(r"^(?:" + "|".join(_GENERIC_STEMS) + r")_?\d+$", re.IGNORECASE)

# Pattern (b): canonical metasyntactic placeholders, as a def/class name only.
_PLACEHOLDERS = frozenset({"foo", "bar", "baz", "qux", "foobar"})

SPEC = RuleSpec(
    rule="ai-slop/generic-naming",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.INFO,
    category="Naming",
    help=(
        "Rename this to something that states its domain meaning. Generic 'stem + number' names "
        "(helper_2, data1, result_3) and metasyntactic placeholders (foo/bar/baz) read as unfinished "
        "AI scaffolding; pick a name that says what the value/function/class actually is."
    ),
    style=True,
    fixable=False,
)

rules: tuple[RuleSpec, ...] = (SPEC,)


def _is_dunder(name: str) -> bool:
    """True for ``__name__``-style dunders (excluded defensively; they never carry empty names)."""
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _classify(name: str, *, allow_placeholder: bool) -> str | None:
    """Return a human reason if ``name`` is an empty AI name under the active patterns, else None."""
    if _is_dunder(name):
        return None
    if _STEM_COUNTER_RE.match(name):
        return "generic stem with a numeric suffix"
    if allow_placeholder and name.lower() in _PLACEHOLDERS:
        return "metasyntactic placeholder name"
    return None


def _module_level_assignment_names(tree: ast.Module) -> list[tuple[str, int]]:
    """Collect ``(name, line)`` for every *module-level* assignment target name.

    Only direct children of the module body are considered (true module scope). Tuple/list unpacking
    targets are flattened so ``data1, data2 = f()`` is covered; attribute/subscript targets are
    ignored (they are not new binding names). Assignments nested in functions/classes are out of
    scope — those are local variables and the rule deliberately does not touch them.
    """
    names: list[tuple[str, int]] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            names.extend(_binding_names(target))
    return names


def _binding_names(target: ast.expr) -> list[tuple[str, int]]:
    """Flatten an assignment target into the simple ``Name`` bindings it introduces."""
    if isinstance(target, ast.Name):
        return [(target.id, target.lineno)]
    if isinstance(target, (ast.Tuple, ast.List)):
        flattened: list[tuple[str, int]] = []
        for element in target.elts:
            flattened.extend(_binding_names(element))
        return flattened
    # Attribute (``self.x``), Subscript (``d[k]``), Starred, etc. bind no new simple name here.
    return []


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Flag empty AI-generated names on defs/classes and module-level assignment targets.

    AST-driven (no text scan), so string/comment content can never self-match. Definitions (anywhere
    in the tree) accept both patterns; module-level assignments accept only the stem+counter pattern.
    """
    findings: list[Diagnostic] = []

    for node in ast.walk(ctx.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            reason = _classify(node.name, allow_placeholder=True)
            if reason is not None:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                findings.append(
                    diagnostic_from_spec(
                        SPEC,
                        file_path=ctx.path.as_posix(),
                        line=node.lineno,
                        message=f"{kind} name '{node.name}' is a {reason} — give it a meaningful, domain-specific name",
                        detail="Empty 'stem+number' / placeholder names read as unfinished AI scaffolding.",
                    )
                )

    for name, line in _module_level_assignment_names(ctx.tree):
        reason = _classify(name, allow_placeholder=False)
        if reason is not None:
            findings.append(
                diagnostic_from_spec(
                    SPEC,
                    file_path=ctx.path.as_posix(),
                    line=line,
                    message=f"module-level name '{name}' is a {reason} — give it a meaningful, domain-specific name",
                    detail="Empty 'stem+number' names at module scope read as unfinished AI scaffolding.",
                )
            )

    return findings
