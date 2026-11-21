"""Import-cycle guard: detect circular import dependencies between ``src/`` modules (Tarjan SCC).

the source monorepo's layering gate (``scripts/check_layering.py``) enforces dependency *direction* across architectural
layers, but it is provably blind to a same-layer cycle — ``modules.a -> modules.b -> modules.a`` — which is
exactly the rot a weak coding agent introduces to "make the import work". This module closes that gap with a
mathematically exact check: a strongly-connected component (SCC) of size > 1 in the module import graph *is*
a cycle, so there are no heuristics and near-zero false positives.

It reuses the Tier-4 resolver primitives (:func:`deadcode_audit.reachability.collect_module_facts` and
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
from typing import TYPE_CHECKING

from deadcode_audit import config, diffscope
from deadcode_audit.framework import safe_parse
from deadcode_audit.reachability import _resolve_relative, module_dotted_name

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _internal_nodes() -> dict[str, Path]:
    """Map every ``src/`` module's dotted name to its file (the import-graph node set)."""
    nodes: dict[str, Path] = {}
    for path in diffscope.all_src_files():
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


def _runtime_imports(tree: ast.Module) -> Iterator[tuple[ast.Import | ast.ImportFrom, bool]]:
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


def _import_targets(
    node: ast.Import | ast.ImportFrom, dotted: str, is_package: bool, nodes: dict[str, Path]
) -> set[str]:
    """Internal modules one import statement depends on."""
    if isinstance(node, ast.Import):
        names = [(alias.name, None) for alias in node.names]
        module = None
    else:
        module = _resolve_relative(dotted, is_package, node.level, node.module)
        names = [(module, None if alias.name == "*" else alias.name) for alias in node.names]
    resolved = (_edge_target(mod, name, nodes) for mod, name in names if mod is not None)
    return {target for target in resolved if target is not None}


def build_import_graph(*, include_lazy: bool = True) -> dict[str, set[str]]:
    """Build the directed runtime import graph over internal ``src/`` modules (node -> imported modules).

    ``if TYPE_CHECKING:`` imports are never edges. ``include_lazy=False`` also drops function-local imports,
    leaving the ``top`` edges that execute while the module is being imported.
    """
    nodes = _internal_nodes()
    graph: dict[str, set[str]] = {dotted: set() for dotted in nodes}
    for dotted, path in nodes.items():
        tree = safe_parse(diffscope.read_text(path))
        if tree is None:
            if include_lazy:  # warn once: the top-only pass re-reads the same files
                print(f"warning: {path.as_posix()} does not parse; treating as no imports", file=sys.stderr)
            continue
        is_package = path.name == "__init__.py"
        targets: set[str] = set()
        for node, lazy in _runtime_imports(tree):
            if include_lazy or not lazy:
                targets |= _import_targets(node, dotted, is_package, nodes)
        targets.discard(dotted)  # a self-import is degenerate, not a cycle
        graph[dotted] = targets
    return graph


def tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return the strongly-connected components of ``graph`` (Tarjan's algorithm).

    Iterative (explicit stack) so a deep import chain cannot blow the Python recursion limit on large trees.
    """
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    counter = 0
    sccs: list[list[str]] = []

    for root in graph:
        if root in index_of:
            continue
        # work stack of (node, iterator over its successors)
        work: list[tuple[str, list[str]]] = [(root, sorted(graph.get(root, ())))]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, successors = work[-1]
            advanced = False
            while successors:
                succ = successors.pop(0)
                if succ not in index_of:
                    index_of[succ] = lowlink[succ] = counter
                    counter += 1
                    stack.append(succ)
                    on_stack[succ] = True
                    work.append((succ, sorted(graph.get(succ, ()))))
                    advanced = True
                    break
                if on_stack.get(succ, False):
                    lowlink[node] = min(lowlink[node], index_of[succ])
            if advanced:
                continue
            # all successors processed: this node is done
            if lowlink[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
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


def _print_json(reported: list[tuple[list[str], str]], allowed: set[str], *, advisory: bool) -> None:
    cycles_json = [
        {"key": _cycle_key(c), "members": c, "kind": kind, "allowed": _cycle_key(c) in allowed} for c, kind in reported
    ]
    print(json.dumps({"advisory": advisory, "count": len(reported), "cycles": cycles_json}, indent=2))


def _print_text(reported: list[tuple[list[str], str]], total: int, allowed: set[str], *, advisory: bool) -> None:
    hard = [c for c, kind in reported if kind == "hard"]
    soft = [c for c, kind in reported if kind == "soft"]
    if hard:
        label = "advisory" if advisory else "BLOCKING"
        print(f"import-cycle check ({label}): {len(hard)} circular import group(s) among src/ modules:")
        for cycle in hard:
            tag = "  [allowlisted]" if advisory and _cycle_key(cycle) in allowed else ""
            print(f"  cycle: {' -> '.join(cycle)} -> {cycle[0]}{tag}")
        print("  -> break the cycle (extract the shared piece, or invert one dependency); allowlist a")
        print("     deliberate cycle in .deadcode.yml under allowed_cycles by its '<a> <-> <b>' key")
    elif not total:
        print("import-cycle check: no circular imports among src/ modules")
    else:
        # Only allowlisted and/or soft cycles exist: claiming zero circular imports would be false advertising.
        note = f" ({len(allowed)} cycle(s) allowlisted in .deadcode.yml)" if allowed else ""
        print(f"import-cycle check: no blocking circular imports{note}")
    if soft:
        print(f"import-cycle check: {len(soft)} soft cycle(s), each broken by a function-local import (not blocking):")
        for cycle in soft:
            tag = "  [allowlisted]" if _cycle_key(cycle) in allowed else ""
            print(f"  cycle: {' -> '.join(cycle)} -> {cycle[0]}{tag}")


def run(*, as_json: bool, advisory: bool, strict_lazy: bool = False) -> int:
    """Report import cycles among ``src/`` modules.

    Advisory mode (``make deadcode-full``) lists EVERY cycle and always exits 0. Blocking mode
    (pre-commit / CI) fails (exit 1) only on HARD cycles NOT in ``.deadcode.yml``'s ``allowed_cycles`` — so a
    codebase with a baselined set of accepted cycles still blocks any NEW one (the ratchet pattern). SOFT
    cycles (broken by a function-local import) are listed but never block unless ``strict_lazy`` is set.
    """
    allowed = set(config.load_deadcode_config(diffscope.REPO_ROOT).allowed_cycles)
    graph = build_import_graph()
    top_graph = graph if strict_lazy else build_import_graph(include_lazy=False)
    all_cycles = classify_cycles(graph, top_graph)
    reported = all_cycles if advisory else [(c, kind) for c, kind in all_cycles if _cycle_key(c) not in allowed]

    if as_json:
        _print_json(reported, allowed, advisory=advisory)
    else:
        _print_text(
            reported, len(all_cycles), {k for c, _ in all_cycles if (k := _cycle_key(c)) in allowed}, advisory=advisory
        )

    blocking = [c for c, kind in all_cycles if kind == "hard" and _cycle_key(c) not in allowed]
    return 0 if advisory or not blocking else 1
