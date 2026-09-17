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

Run blocking (exit 1 on any non-allowed cycle) for the pre-commit/CI net, or ``--advisory`` (always exit 0)
for the ``make deadcode-full`` whole-tree audit.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from deadcode_audit import config, diffscope
from deadcode_audit.reachability import collect_module_facts, module_dotted_name

if TYPE_CHECKING:
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


def build_import_graph() -> dict[str, set[str]]:
    """Build the directed import graph over internal ``src/`` modules (node -> imported internal modules)."""
    nodes = _internal_nodes()
    graph: dict[str, set[str]] = {dotted: set() for dotted in nodes}
    for dotted, path in nodes.items():
        facts = collect_module_facts(diffscope.read_text(path), path)
        targets: set[str] = set()
        for module, name, _asname in facts.from_imports:
            resolved = _edge_target(module, name, nodes)
            if resolved is not None:
                targets.add(resolved)
        for module, _asname in facts.plain_imports:
            resolved = _edge_target(module, None, nodes)
            if resolved is not None:
                targets.add(resolved)
        for module in facts.star_from:
            resolved = _edge_target(module, None, nodes)
            if resolved is not None:
                targets.add(resolved)
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


def run(*, as_json: bool, advisory: bool) -> int:
    """Report import cycles among ``src/`` modules.

    Advisory mode (``make deadcode-full``) lists EVERY cycle and always exits 0. Blocking mode
    (pre-commit / CI) fails (exit 1) only on cycles NOT in ``.deadcode.yml``'s ``allowed_cycles`` — so a
    codebase with a baselined set of accepted cycles still blocks any NEW one (the ratchet pattern).
    """
    allowed = set(config.load_deadcode_config(diffscope.REPO_ROOT).allowed_cycles)
    all_cycles = find_cycles(build_import_graph())
    blocking_cycles = [cycle for cycle in all_cycles if _cycle_key(cycle) not in allowed]
    reported = all_cycles if advisory else blocking_cycles

    if as_json:
        print(
            json.dumps(
                {
                    "advisory": advisory,
                    "count": len(reported),
                    "cycles": [
                        {
                            "key": _cycle_key(c),
                            "members": c,
                            "allowed": _cycle_key(c) in allowed,
                        }
                        for c in reported
                    ],
                },
                indent=2,
            )
        )
    elif not reported:
        if all_cycles:
            # Only-allowlisted cycles exist: claiming zero circular imports would be false advertising.
            print(
                f"import-cycle check: no blocking circular imports "
                f"({len(all_cycles)} cycle(s) allowlisted in .deadcode.yml)"
            )
        else:
            print("import-cycle check: no circular imports among src/ modules")
    else:
        kind = "advisory" if advisory else "BLOCKING"
        print(f"import-cycle check ({kind}): {len(reported)} circular import group(s) among src/ modules:")
        for cycle in reported:
            tag = "  [allowlisted]" if advisory and _cycle_key(cycle) in allowed else ""
            print(f"  cycle: {' -> '.join(cycle)} -> {cycle[0]}{tag}")
        print("  -> break the cycle (extract the shared piece, or invert one dependency); allowlist a")
        print("     deliberate cycle in .deadcode.yml under allowed_cycles by its '<a> <-> <b>' key")

    if advisory or not blocking_cycles:
        return 0
    return 1
