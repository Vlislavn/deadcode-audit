"""Tier 6a: redundant-transform detection + the diff-scoped Tier 6 orchestration.

Collapsible call compositions (``sorted(sorted(x))``, ``list(tuple(x))``,
``dict(list(d.items()))``, ``set(sorted(x))`` ...), identity comprehensions
(``[x for x in it]``, ``{k: v for k, v in d.items()}``) and identity maps
(``list(map(lambda a: a, it))``).

The rules encode *semantic principles* — idempotency (``f(f(x)) == f(x)``), inverse /
no-op composition (``outer(inner(x)) == outer(x)``), and identity transforms — not a
catalogue of specific snippets, so they generalise beyond any motivating example.
Nothing that changes observable behaviour (e.g. ``list(set(x))`` which de-duplicates) is
included. ``run_check`` also runs the advisory near-duplicate pass from :mod:`clones`.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

from deadcode_audit import clones, diffscope
from deadcode_audit.framework import safe_parse

# --- Redundant transform tables (semantic-principle driven, conservative) ---

# Callables redundant under SELF-application ``f(f(x)) == f(x)``. Self-application is only
# flagged when both calls are a *simple* ``f(x)`` (one positional arg, no keywords) — so the
# multi-key stable-sort idiom ``sorted(sorted(rows, key=a), key=b)`` and ``dict(dict(x), k=v)``
# are NOT touched. Grouped by the principle that makes them idempotent:
IDEMPOTENT_CALLABLES = frozenset(
    {
        "sorted",
        "set",
        "frozenset",
        "abs",
        "round",  # mathematically idempotent (round only with no extra ndigits arg — enforced by the simple-call guard)
        "bool",
        "str",
        "int",
        "float",  # idempotent total self-coercions: f(f(x)) == f(x)
        "list",
        "tuple",
        "dict",  # redundant re-construction of the same container type
    }
)

# CROSS-callable ``outer(inner(x)) == outer(x)``: the inner call is a no-op given the outer one.
# Self pairs live in IDEMPOTENT_CALLABLES above. Only behaviour-preserving pairs are listed;
# de-duplicating/reordering/type-changing pairs (e.g. ``list(set(x))``) are deliberately absent.
COLLAPSIBLE_COMPOSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # round-trip conversions: inner container is rebuilt by outer with no effect
        ("list", "tuple"),
        ("tuple", "list"),
        ("dict", "list"),
        ("dict", "tuple"),
        # unordered outer makes an inner ordering/listing pointless
        ("set", "list"),
        ("set", "tuple"),
        ("set", "sorted"),
        ("frozenset", "list"),
        ("frozenset", "tuple"),
        ("frozenset", "sorted"),
        # sorting already-materialised iterables: the inner listing is wasted
        ("sorted", "list"),
        ("sorted", "tuple"),
    }
)

# Outers for which ``outer(map(lambda a: a, it))`` is a redundant composition: the identity map
# yields exactly ``it``'s elements, so ``list/set/tuple`` of it collapses to applying the outer to
# ``it`` directly. For any other callable (e.g. ``print(map(lambda a: a, it))``) the wrapper is NOT
# redundant — only the bare ``map`` rule below applies.
_IDENTITY_MAP_CONSUMERS = frozenset({"list", "set", "tuple"})

# UnaryOp operators that are their own inverse / a no-op: ``-(-x)``, ``~~x``, ``+(+x)``.
_INVOLUTIVE_UNARY_OPS: dict[type[ast.unaryop], str] = {ast.USub: "-", ast.UAdd: "+", ast.Invert: "~"}


@dataclass(frozen=True)
class RedundancyFinding:
    """A redundant transformation detected on a single source line."""

    file_path: Path
    line: int
    kind: str
    message: str


def _call_name(node: ast.expr) -> str | None:
    """Return the bare callable name for ``f(...)`` / module-less calls, else None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _is_simple_unary_call(node: ast.expr) -> bool:
    """True for ``f(x)`` — exactly one positional argument, no keywords, no ``*args``.

    Self-application is only redundant when neither call carries extra arguments that could
    change the relation (e.g. ``sorted``'s ``key=``/``reverse=``, a second ``dict`` mapping).
    """
    return (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
        and not isinstance(node.args[0], ast.Starred)
    )


def _is_identity_lambda(node: ast.expr) -> bool:
    """True for ``lambda a: a`` (single arg returned unchanged)."""
    if not isinstance(node, ast.Lambda):
        return False
    args = node.args
    if len(args.args) != 1 or args.vararg or args.kwarg or args.kwonlyargs:
        return False
    return isinstance(node.body, ast.Name) and node.body.id == args.args[0].arg


def _comprehension_is_identity(generators: list[ast.comprehension], element: ast.expr) -> bool:
    """True when a comprehension reproduces its input element unchanged with no filtering.

    Handles ``[x for x in it]`` (element is the loop target) and the unpacked
    ``{k: v for k, v in it}`` style via :func:`_dictcomp_is_identity`.
    """
    if len(generators) != 1:
        return False
    gen = generators[0]
    if gen.ifs or gen.is_async:
        return False
    target = gen.target
    return isinstance(target, ast.Name) and isinstance(element, ast.Name) and element.id == target.id


def _dictcomp_is_identity(node: ast.DictComp) -> bool:
    """True for ``{k: v for k, v in it}`` — keys/values reproduced unchanged, no filtering."""
    if len(node.generators) != 1:
        return False
    gen = node.generators[0]
    if gen.ifs or gen.is_async:
        return False
    target = gen.target
    if not (isinstance(target, ast.Tuple) and len(target.elts) == 2):
        return False
    names = [elt.id for elt in target.elts if isinstance(elt, ast.Name)]
    if len(names) != 2:
        return False
    key, value = node.key, node.value
    return isinstance(key, ast.Name) and isinstance(value, ast.Name) and key.id == names[0] and value.id == names[1]


class _RedundancyVisitor(ast.NodeVisitor):
    """Collect redundant-transform findings from one module AST."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[RedundancyFinding] = []

    def visit_Call(self, node: ast.Call) -> None:
        outer = _call_name(node)
        if outer is not None and node.args:
            inner_node = node.args[0]
            inner = _call_name(inner_node)
            if inner is not None and outer == inner and outer in IDEMPOTENT_CALLABLES:
                # Self-application: only redundant when neither call carries extra args
                # (guards the multi-key ``sorted(sorted(x, key=a), key=b)`` / ``dict(dict(x), k=v)`` idioms).
                if _is_simple_unary_call(node) and _is_simple_unary_call(inner_node):
                    self.findings.append(
                        RedundancyFinding(
                            self.path,
                            node.lineno,
                            "collapsible-composition",
                            f"{outer}({outer}(...)) is reducible to {outer}(...) — redundant self-application",
                        )
                    )
            elif inner is not None and (outer, inner) in COLLAPSIBLE_COMPOSITIONS:
                self.findings.append(
                    RedundancyFinding(
                        self.path,
                        node.lineno,
                        "collapsible-composition",
                        f"{outer}({inner}(...)) is reducible to {outer}(...) — redundant {inner}() inside {outer}()",
                    )
                )
            # identity map: list/set/tuple(map(lambda a: a, it)) or bare map(...)
            if (
                inner == "map"
                and outer in _IDENTITY_MAP_CONSUMERS
                and isinstance(inner_node, ast.Call)
                and inner_node.args
                and _is_identity_lambda(inner_node.args[0])
            ):
                self.findings.append(
                    RedundancyFinding(
                        self.path, node.lineno, "identity-map", f"{outer}(map(lambda a: a, ...)) is an identity map"
                    )
                )
        if outer == "map" and node.args and _is_identity_lambda(node.args[0]):
            self.findings.append(
                RedundancyFinding(
                    self.path, node.lineno, "identity-map", "map(lambda a: a, ...) returns its input unchanged"
                )
            )
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        operand = node.operand
        if (
            isinstance(operand, ast.UnaryOp)
            and type(node.op) is type(operand.op)
            and type(node.op) in _INVOLUTIVE_UNARY_OPS
        ):
            symbol = _INVOLUTIVE_UNARY_OPS[type(node.op)]
            self.findings.append(
                RedundancyFinding(
                    self.path,
                    node.lineno,
                    "involution",
                    f"{symbol}{symbol}x is a no-op (the {symbol!r} operator is its own inverse / identity)",
                )
            )
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        if _comprehension_is_identity(node.generators, node.elt):
            self.findings.append(
                RedundancyFinding(
                    self.path, node.lineno, "identity-comprehension", "[x for x in it] is reducible to list(it)"
                )
            )
        self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        if _comprehension_is_identity(node.generators, node.elt):
            self.findings.append(
                RedundancyFinding(
                    self.path, node.lineno, "identity-comprehension", "{x for x in it} is reducible to set(it)"
                )
            )
        self.generic_visit(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        if _comprehension_is_identity(node.generators, node.elt):
            self.findings.append(
                RedundancyFinding(
                    self.path, node.lineno, "identity-comprehension", "(x for x in it) is reducible to iter(it)"
                )
            )
        self.generic_visit(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        if _dictcomp_is_identity(node):
            self.findings.append(
                RedundancyFinding(
                    self.path, node.lineno, "identity-comprehension", "{k: v for k, v in it} is reducible to dict(it)"
                )
            )
        self.generic_visit(node)


def find_redundant_transforms(source: str, path: Path) -> list[RedundancyFinding]:
    """Return redundant-transform findings for one module's source.

    An unparseable file (syntax error, BOM, null bytes, pathological nesting) yields no findings
    plus a stderr warning — one malformed file must never crash the caller with a traceback.
    """
    tree = safe_parse(source)
    if tree is None:
        print(f"warning: {path.as_posix()} does not parse; skipping redundancy analysis", file=sys.stderr)
        return []
    visitor = _RedundancyVisitor(path)
    visitor.visit(tree)
    return sorted(visitor.findings, key=lambda f: (f.line, f.kind))


def run_check(compare_branch: str, *, threshold: float, min_tokens: int) -> int:
    """Diff-scoped redundancy + clone advisory. Returns non-zero when findings exist."""
    changed_files = diffscope.changed_src_python_files(compare_branch)
    if not changed_files:
        print("No changed src files for redundancy scan")
        return 0

    changed_lines = diffscope.added_lines_by_file(compare_branch, changed_files)

    redundancies: list[RedundancyFinding] = []
    for path in changed_files:
        lines = changed_lines.get(path.as_posix(), set())
        for finding in find_redundant_transforms(diffscope.read_text(path), path):
            if finding.line in lines:
                redundancies.append(finding)

    # Clone detection: changed functions (touching a changed line) vs the whole src corpus.
    corpus: list[clones.FuncRecord] = []
    for path in diffscope.all_src_files():
        corpus.extend(clones.collect_functions(diffscope.read_text(path), path))
    changed_set = {p.as_posix() for p in changed_files}
    targets = [
        rec
        for rec in corpus
        if rec.file_path.as_posix() in changed_set and rec.line in changed_lines.get(rec.file_path.as_posix(), set())
    ]
    clone_findings = clones.find_near_duplicate_functions(targets, corpus, threshold=threshold, min_tokens=min_tokens)

    # Clones are ADVISORY: parallel interface implementations are legitimately structural
    # duplicates, so surfacing-for-review (not hard-blocking) is the right severity.
    # Collapse symmetric A~B / B~A pairs (both can be in the changed set) to one line.
    seen_pairs: set[frozenset[tuple[str, int]]] = set()
    unique_clones: list[clones.CloneFinding] = []
    for clone in clone_findings:
        key = frozenset({(clone.file_path.as_posix(), clone.line), (clone.other_file.as_posix(), clone.other_line)})
        if key not in seen_pairs:
            seen_pairs.add(key)
            unique_clones.append(clone)
    if unique_clones:
        print(f"[advisory, non-blocking] {len(unique_clones)} changed function(s) closely mirror an existing one:")
        for clone in unique_clones:
            print(
                f"  {clone.file_path.as_posix()}:{clone.line} '{clone.symbol}' ~= "
                f"{clone.other_file.as_posix()}:{clone.other_line} '{clone.other_symbol}' "
                f"(similarity {clone.similarity}) -> consider extracting a shared/parameterised helper"
            )
    # Redundant transforms are high-precision and behaviour-preserving to simplify, so they BLOCK.
    if redundancies:
        print(f"[block] {len(redundancies)} reducible transformation(s) on changed lines:")
        for finding in redundancies:
            print(f"  {finding.file_path.as_posix()}:{finding.line}: {finding.message}")
        return 1
    print("Redundancy scan passed (no reducible transforms on changed lines)")
    return 0


def run_files(paths: list[str]) -> int:
    """Pre-commit mode: block on reducible transforms in the given (staged) files.

    Only the high-precision, behaviour-preserving redundant-transform check runs here — it is
    per-file, so it suits pre-commit's staged-file model. Clone detection needs the whole-corpus
    and is advisory, so it stays in the diff-scoped ``redundancy`` / CI path.
    """
    findings: list[RedundancyFinding] = []
    for raw in paths:
        path = Path(raw)
        if path.suffix != ".py" or not path.exists():
            continue
        # utf-8-sig: tolerate UTF-8-BOM staged files (a BOM read as plain utf-8 surfaces as a
        # SyntaxError inside ast.parse).
        findings.extend(find_redundant_transforms(path.read_text(encoding="utf-8-sig"), path))
    if not findings:
        return 0
    print(f"[block] {len(findings)} reducible transformation(s):")
    for finding in findings:
        print(f"  {finding.file_path.as_posix()}:{finding.line}: {finding.message}")
    return 1
