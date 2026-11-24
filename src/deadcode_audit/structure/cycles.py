"""Import-cycle guard: detect circular import dependencies between ``src/`` modules (Tarjan SCC).

the source monorepo's layering gate (``scripts/check_layering.py``) enforces dependency *direction* across architectural
layers, but it is provably blind to a same-layer cycle — ``modules.a -> modules.b -> modules.a`` — which is
exactly the rot a weak coding agent introduces to "make the import work". This module closes that gap with a
mathematically exact check: a strongly-connected component (SCC) of size > 1 in the module import graph *is*
a cycle, so there are no heuristics and near-zero false positives.

It reuses the Tier-4 resolver primitives (:func:`deadcode_audit.structure.reachability.collect_module_facts` and
``module_dotted_name``) to build the graph from real import edges — direct, aliased, relative, ``import a.b``,
and ``from a import b`` (resolved to the submodule when ``a.b`` is itself an internal module). The algorithm
follows the iterative-context Tarjan SCC reference in ``scripts/agents/_sig_builder_core.py`` (modelled, not
imported). Cycles known and intentionally accepted can be listed in ``.deadcode.yml`` under ``allowed_cycles``.

Only runtime imports are edges: anything under ``if TYPE_CHECKING:`` never executes and is ignored. Each edge
is ``top`` (module level) or ``lazy`` (nested in a function body). A cycle that survives with only ``top``
edges is HARD (it fails at import time); one that needs a lazy edge is SOFT (a function-local import already
breaks it). ``--strict-lazy`` counts lazy edges as hard again.

Run blocking (exit 1 on any non-allowed HARD cycle) for the pre-commit/CI net, or ``--advisory`` (always exit
0) for the ``make deadcode-full`` whole-tree audit.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import deque
from typing import TYPE_CHECKING, NamedTuple

from deadcode_audit import config, layout
from deadcode_audit.framework import safe_parse
from deadcode_audit.structure.reachability import (
    _resolve_relative,
    capped,
    module_dotted_name,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


HELD_IN_MIND = 7  # lists of ties / circle nodes longer than this are capped with '+N more'


def _internal_nodes() -> dict[str, Path]:
    """Map every ``src/`` module's dotted name to its file (the import-graph node set)."""
    nodes: dict[str, Path] = {}
    for path in layout.runtime_files():
        dotted = module_dotted_name(path)
        if dotted is not None:
            nodes[dotted] = path
    return nodes


def _edge_target(module: str, name: str | None, nodes: dict[str, Path]) -> str | None:
    """Resolve one import to the internal module it depends on, or None if external.

    For ``from <module> import <name>`` the dependency is the submodule ``<module>.<name>`` when that is
    itself an internal module (so ``from modules.core import config`` depends on ``modules.core.config``,
    not merely the package); otherwise it is ``<module>`` (a symbol import). ``import <module>`` and
    ``from <module> import *`` pass ``name=None`` and resolve straight to ``<module>``.
    """
    if name is not None:
        specific = f"{module}.{name}"
        if specific in nodes:
            return specific
    return module if module in nodes else None


def _is_type_checking(test: ast.expr) -> bool:
    """True for ``TYPE_CHECKING`` / ``typing.TYPE_CHECKING`` (any alias of the typing module)."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_imports(
    tree: ast.Module,
) -> Iterator[tuple[ast.Import | ast.ImportFrom, bool]]:
    """Yield ``(import node, lazy)`` for every import that can run, skipping ``if TYPE_CHECKING:`` bodies.

    ``lazy`` is True when the import is nested in a function or method body. The ``else`` branch of a
    ``TYPE_CHECKING`` test is runtime and is kept.
    """
    stack: list[tuple[ast.AST, bool]] = [(stmt, False) for stmt in tree.body]
    while stack:
        node, lazy = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node, lazy
        elif isinstance(node, ast.If) and _is_type_checking(node.test):
            stack.extend((stmt, lazy) for stmt in node.orelse)
        else:
            inner = lazy or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            stack.extend((child, inner) for child in ast.iter_child_nodes(node))


def _attribute_uses(tree: ast.Module) -> dict[str, set[str]]:
    """Map ``<dotted expr>`` -> distinct attributes read from it (``m.a``, ``m.b`` -> ``{"m": {"a", "b"}}``)."""
    uses: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, (ast.Name, ast.Attribute)):
            uses.setdefault(ast.unparse(node.value), set()).add(node.attr)
    return uses


def _import_names(
    node: ast.Import | ast.ImportFrom,
    dotted: str,
    is_package: bool,
    nodes: dict[str, Path],
    uses: dict[str, set[str]],
) -> Iterator[tuple[str, set[str]]]:
    """Yield ``(internal module, imported names)`` for one import statement.

    ``from m import a, b`` names ``a``/``b``; ``import m`` names the distinct ``m.attr`` uses (or ``m`` itself,
    cost 1, when no attribute use is resolvable).
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            target = _edge_target(alias.name, None, nodes)
            if target is not None:
                yield (
                    target,
                    set(uses.get(alias.asname or alias.name, ())) or {alias.name},
                )
        return
    module = _resolve_relative(dotted, is_package, node.level, node.module)
    for alias in node.names if module is not None else ():
        target = _edge_target(module, None if alias.name == "*" else alias.name, nodes)
        if target is not None:
            yield target, {alias.name}


class Tie(NamedTuple):
    """One import edge ``src -> dst``: the imported names (its cost) and the first ``(file, line)`` site."""

    src: str
    dst: str
    lazy: bool
    names: frozenset[str]
    site: tuple[str, int]


def collect_ties() -> tuple[list[Tie], int]:
    """Walk every ``src/`` module's runtime imports ONCE: one :class:`Tie` per ``(src, dst, lazy)`` edge.

    Also returns the number of files that did not parse (they contribute no edges, so the graph is incomplete).
    """
    nodes = _internal_nodes()
    acc: dict[tuple[str, str, bool], Tie] = {}
    unparsed = 0
    for dotted, path in nodes.items():
        tree = safe_parse(layout.read_text(path))
        if tree is None:
            print(
                f"warning: {path.as_posix()} does not parse; treating as no imports",
                file=sys.stderr,
            )
            unparsed += 1
            continue
        uses = _attribute_uses(tree)
        for node, lazy in _runtime_imports(tree):
            for target, names in _import_names(node, dotted, path.name == "__init__.py", nodes, uses):
                if target != dotted:  # a self-import is degenerate, not a cycle
                    tie = Tie(
                        dotted,
                        target,
                        lazy,
                        frozenset(names),
                        (path.as_posix(), node.lineno),
                    )
                    acc[(dotted, target, lazy)] = _merge(acc.get((dotted, target, lazy)), tie)
    return list(acc.values()), unparsed


def _merge(old: Tie | None, new: Tie) -> Tie:
    """Combine two ties of the same edge: union of names, earliest site, lazy only if both are."""
    if old is None:
        return new
    return Tie(
        new.src,
        new.dst,
        old.lazy and new.lazy,
        old.names | new.names,
        min(old.site, new.site),
    )


def build_import_graph(*, include_lazy: bool = True, ties: list[Tie] | None = None) -> dict[str, set[str]]:
    """Derive the directed runtime import graph (node -> imported modules) from the import ties.

    ``if TYPE_CHECKING:`` imports are never edges. ``include_lazy=False`` also drops function-local imports,
    leaving the ``top`` edges that execute while the module is being imported.
    """
    graph: dict[str, set[str]] = {}
    for tie in collect_ties()[0] if ties is None else ties:
        graph.setdefault(tie.dst, set())
        edges = graph.setdefault(tie.src, set())
        if include_lazy or not tie.lazy:
            edges.add(tie.dst)
    return graph


def tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return the strongly-connected components of ``graph`` (Tarjan's algorithm).

    Iterative (explicit stack) so a deep import chain cannot blow the Python recursion limit on large trees.
    """
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    sccs: list[list[str]] = []
    work: list[tuple[str, list[str]]] = []  # (node, its unprocessed successors)

    def visit(node: str) -> None:
        index_of[node] = lowlink[node] = len(index_of)
        stack.append(node)
        on_stack[node] = True
        work.append((node, sorted(graph.get(node, ()))))

    def next_unvisited(node: str, successors: list[str]) -> str | None:
        while successors:
            succ = successors.pop(0)
            if succ not in index_of:
                return succ
            if on_stack.get(succ, False):
                lowlink[node] = min(lowlink[node], index_of[succ])
        return None

    for root in graph:
        if root in index_of:
            continue
        visit(root)
        while work:
            node, successors = work[-1]
            succ = next_unvisited(node, successors)
            if succ is not None:
                visit(succ)
                continue
            # all successors processed: this node is done
            if lowlink[node] == index_of[node]:
                component = [stack.pop()]
                while component[-1] != node:
                    component.append(stack.pop())
                on_stack.update(dict.fromkeys(component, False))
                sccs.append(component)
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return sccs


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return every import cycle: each SCC of size > 1, as a sorted member list, deterministically ordered."""
    cycles = [sorted(scc) for scc in tarjan_scc(graph) if len(scc) > 1]
    return sorted(cycles)


def _cycle_key(cycle: list[str]) -> str:
    """A stable identifier for a cycle (sorted members joined) for the ``allowed_cycles`` allowlist."""
    return " <-> ".join(sorted(cycle))


def classify_cycles(graph: dict[str, set[str]], top_graph: dict[str, set[str]]) -> list[tuple[list[str], str]]:
    """Return ``(cycle, kind)`` pairs, HARD first. HARD = the cycle also exists using only ``top_graph`` edges."""
    hard_members = {member for scc in find_cycles(top_graph) for member in scc}
    classified = [(cycle, "hard" if hard_members.intersection(cycle) else "soft") for cycle in find_cycles(graph)]
    return sorted(classified, key=lambda item: item[1] != "hard")  # stable: HARD first, order kept within a kind


class Found(NamedTuple):
    """A reported cycle: members, kind (``hard``/``soft``/``package``), ties cheapest first, shortest circle."""

    cycle: list[str]
    kind: str
    ties: list[Tie]
    circle: list[str]


def _covered(cycle: list[str], allowed: list[set[str]]) -> bool:
    """True when the cycle's members are a SUBSET of some ``allowed_cycles`` entry's members."""
    return any(set(cycle) <= entry for entry in allowed)


def _circle_report(cycle: list[str], edges: dict[tuple[str, str], Tie]) -> tuple[list[Tie], list[str]]:
    """Ties inside ``cycle`` (cheapest first) and the shortest circle through the cheapest one (BFS in tie order)."""
    ties = sorted(
        (edges[u, v] for u in cycle for v in cycle if (u, v) in edges),
        key=lambda t: (len(t.names), t.src, t.dst),
    )
    if not ties:
        return [], []
    first, successors = ties[0], {}
    for tie in ties:
        successors.setdefault(tie.src, []).append(tie.dst)
    prev: dict[str, str | None] = {first.dst: None}
    queue = deque([first.dst])
    while queue and first.src not in prev:
        node = queue.popleft()
        for nxt in successors.get(node, ()):
            if nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    if first.src not in prev:
        return ties, []
    path: list[str] = []
    node = first.src
    while node is not None:  # walk back u -> ... -> v, then close the circle at u
        path.append(node)
        node = prev[node]
    return ties, [first.src, *reversed(path)]


def _edge_map(ties: list[Tie]) -> dict[tuple[str, str], Tie]:
    edges: dict[tuple[str, str], Tie] = {}
    for tie in ties:
        edges[tie.src, tie.dst] = _merge(edges.get((tie.src, tie.dst)), tie)
    return edges


def _blocker_edges(
    cycle: list[str], top_sccs: list[list[str]], top_edges: dict[tuple[str, str], Tie]
) -> dict[tuple[str, str], Tie]:
    """The edges that make a HARD cycle hard: top-level edges inside a top-only SCC contained in ``cycle``."""
    members = set(cycle)
    return {
        (u, v): top_edges[u, v]
        for scc in top_sccs
        if members.issuperset(scc)
        for u in scc
        for v in scc
        if (u, v) in top_edges
    }


def _lift(tie: Tie) -> Tie | None:
    """Lift a file edge to the children of the lowest common ancestor of its ends (None if one contains the other)."""
    src, dst = tie.src.split("."), tie.dst.split(".")
    depth = next((i for i, (a, b) in enumerate(zip(src, dst, strict=False)) if a != b), None)
    if depth is None:
        return None
    return tie._replace(src=".".join(src[: depth + 1]), dst=".".join(dst[: depth + 1]))


def package_cycles(ties: list[Tie], file_cycles: list[list[str]]) -> list[Found]:
    """Cycles between sibling packages: file edges lifted to the children of their LCA, SCC per parent.

    A cycle whose members are exactly a file-level cycle's modules is already reported there and dropped.
    """
    edges: dict[tuple[str, str], Tie] = {}
    for tie in ties:
        if (lifted := _lift(tie)) is not None:
            edges[lifted.src, lifted.dst] = _merge(edges.get((lifted.src, lifted.dst)), lifted)
    parents: dict[str, dict[str, set[str]]] = {}
    for src, dst in edges:
        parents.setdefault(src.rpartition(".")[0], {}).setdefault(src, set()).add(dst)
    known = {frozenset(c) for c in file_cycles}
    found = [Found(c, "package", *_circle_report(c, edges)) for _, g in sorted(parents.items()) for c in find_cycles(g)]
    return [f for f in found if frozenset(f.cycle) not in known]


def _site(tie: Tie) -> str:
    return f"{tie.site[0]}:{tie.site[1]}"


def _print_json(found: list[Found], allowed: list[set[str]], *, advisory: bool, unparsed: int) -> None:
    cycles_json = [
        {
            "key": _cycle_key(f.cycle),
            "members": f.cycle,
            "kind": f.kind,
            "allowed": _covered(f.cycle, allowed),
            "ties": [
                {
                    "src": t.src,
                    "dst": t.dst,
                    "cost": len(t.names),
                    "names": sorted(t.names),
                    "site": _site(t),
                }
                for t in f.ties[:HELD_IN_MIND]
            ],
            "circle": f.circle,
        }
        for f in found
    ]
    report = {
        "advisory": advisory,
        "count": len(found),
        "unparsed": unparsed,
        "cycles": cycles_json,
    }
    print(json.dumps(report, indent=2))


def _print_cycle(f: Found, allowed: list[set[str]], *, advisory: bool) -> None:
    tag = "  [allowlisted]" if advisory and _covered(f.cycle, allowed) else ""
    label = "package cycle" if f.kind == "package" else "cycle"
    print(f"  {label}{tag}")
    print(f"    members: {capped(f.cycle, HELD_IN_MIND)}")
    if not f.ties:
        return
    cut = f.ties[0]
    print(f"    cheapest cut: {cut.src} -> {cut.dst} ({len(cut.names)} name{'s' * (len(cut.names) != 1)}) {_site(cut)}")
    if f.circle:
        print(f"    circle: {capped(f.circle, HELD_IN_MIND).replace(', ', ' -> ')}")
    print(f"    ties: {capped([f'{t.src} -> {t.dst} [{len(t.names)}] {_site(t)}' for t in f.ties], HELD_IN_MIND)}")


def _incomplete(unparsed: int) -> str:
    return f"{unparsed} file(s) did not parse; result incomplete"


def _print_text(
    found: list[Found],
    total: int,
    n_allowed: int,
    allowed: list[set[str]],
    *,
    advisory: bool,
    unparsed: int,
) -> None:
    hard = [f for f in found if f.kind == "hard"]
    soft = [f for f in found if f.kind == "soft"]
    package = [f for f in found if f.kind == "package"]
    if hard:
        label = "advisory" if advisory else "BLOCKING"
        print(f"import-cycle check ({label}): {len(hard)} circular import group(s) among src/ modules:")
        for f in hard:
            _print_cycle(f, allowed, advisory=advisory)
        print("  -> break the cycle (extract the shared piece, or invert one dependency); allowlist a")
        print("     deliberate cycle in .deadcode.yml under allowed_cycles by its '<a> <-> <b>' key")
    elif not total and unparsed:
        print(f"import-cycle check: no circular imports found among the parsed modules; {_incomplete(unparsed)}")
    elif not total:
        print("import-cycle check: no circular imports among src/ modules")
    else:
        # Only allowlisted and/or soft cycles exist: claiming zero circular imports would be false advertising.
        note = f" ({n_allowed} cycle(s) allowlisted in .deadcode.yml)" if n_allowed else ""
        print(f"import-cycle check: no blocking circular imports{note}")
    if unparsed and total:
        print(f"import-cycle check: {_incomplete(unparsed)}")
    if soft:
        print(f"import-cycle check: {len(soft)} soft cycle(s), each broken by a function-local import (not blocking):")
        for f in soft:
            _print_cycle(f, allowed, advisory=True)
    if package:
        print(
            f"import-cycle check: {len(package)} package-level cycle(s) between sibling packages (advisory, not blocking):"
        )
        for f in package:
            _print_cycle(f, allowed, advisory=True)


def run(*, as_json: bool, advisory: bool, strict_lazy: bool = False) -> int:
    """Report import cycles among ``src/`` modules.

    Advisory mode (``make deadcode-full``) lists EVERY cycle and always exits 0. Blocking mode
    (pre-commit / CI) fails (exit 1) only on HARD cycles NOT covered by ``.deadcode.yml``'s ``allowed_cycles``
    (an entry covers any cycle whose members are a subset of its own) — so a codebase with a baselined set of
    accepted cycles still blocks any NEW one (the ratchet pattern). SOFT cycles (broken by a function-local
    import) are listed but never block unless ``strict_lazy`` is set; package-level cycles are advisory only.
    """
    allowed = [set(entry.split(" <-> ")) for entry in config.load_deadcode_config(layout.REPO_ROOT).allowed_cycles]
    ties, unparsed = collect_ties()
    graph = build_import_graph(ties=ties)
    top_graph = graph if strict_lazy else build_import_graph(ties=ties, include_lazy=False)
    edges = _edge_map(ties)
    top_edges = edges if strict_lazy else _edge_map([t for t in ties if not t.lazy])
    top_sccs = find_cycles(top_graph)
    file_found = [
        Found(
            c,
            kind,
            *_circle_report(c, _blocker_edges(c, top_sccs, top_edges) if kind == "hard" else edges),
        )
        for c, kind in classify_cycles(graph, top_graph)
    ]
    all_found = [*file_found, *package_cycles(ties, [f.cycle for f in file_found])]
    reported = all_found if advisory else [f for f in all_found if not _covered(f.cycle, allowed)]

    if as_json:
        _print_json(reported, allowed, advisory=advisory, unparsed=unparsed)
    else:
        n_allowed = sum(_covered(f.cycle, allowed) for f in file_found)
        _print_text(
            reported,
            len(file_found),
            n_allowed,
            allowed,
            advisory=advisory,
            unparsed=unparsed,
        )

    blocking = [f for f in file_found if f.kind == "hard" and not _covered(f.cycle, allowed)]
    return 0 if advisory or not blocking else 1
