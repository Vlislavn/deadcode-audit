"""On-demand, advisory WHOLE-TREE dead-symbol reachability scan (beyond the diff-scoped Tier 4 gate).

Where :func:`deadcode_audit.structure.reachability.runtime_consumer_check` checks only the CHANGED public symbols
and BLOCKS truly-orphaned ones, this scans EVERY public top-level symbol in ``src/`` and reports the ones
with no resolved runtime consumer, ranked by deadness confidence. Like the ``overlaps`` subcommand it is
on-demand and advisory: :func:`run` ALWAYS returns 0. Its actionable classes (orphaned + test-only-reachable)
also feed the ``scan``/``ci`` pass as the :data:`DEAD_SYMBOL_RULE` diagnostic via :func:`dead_symbol_diagnostics`.

It reuses the Tier 4 resolver and the SHARED :func:`deadcode_audit.structure.reachability.classify_unresolved`
classifier, so every symbol lands in the SAME confidence classes the gate uses (plus the undecorated-orphan
top tier). The occurrence floor is computed DIFFERENTLY from the gate on purpose: the gate's ``git grep``
floor sees only TRACKED files and counts non-``.py`` doc hits — fine commit-time/diff-scoped, but wrong for a
whole-tree scan run mid-development (the whole ``scripts/deadcode`` package is untracked as it is built, and
a README mention is not a runtime consumer). So the scan builds an in-memory occurrence index over the
``.py`` corpus it already reads for the resolver: untracked-aware (``rglob``, not ``git grep``), ``.py``-only,
and zero subprocess.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path

from deadcode_audit import diffscope
from deadcode_audit.diagnostic import (
    ENGINE_REACHABILITY,
    Diagnostic,
    RuleSpec,
    Severity,
    diagnostic_from_spec,
)
from deadcode_audit.framework import safe_parse
from deadcode_audit.structure.reachability import (
    ADVISORY_LABEL,
    ADVISORY_RANK,
    ADVISORY_TEST_ONLY,
    PublicSymbolDefinition,
    build_reexport_map,
    classify_unresolved,
    collect_module_facts,
    entry_point_targets,
    module_dotted_name,
    public_symbol_definitions,
    resolved_reference_exists,
    runtime_corpus_files,
    vulture_whitelist_names,
)

# Whole-word identifier tokenizer == ``git grep -w`` semantics: a name "occurs" only as a complete
# token (so ``reset_for_tests`` does NOT match inside ``_reset_for_tests``). Run over RAW lines, it also
# sees names in strings/comments — exactly as the grep floor did — so dynamic ``getattr("name")`` /
# string-registry use still demotes a symbol out of the orphaned tier.
_IDENT = re.compile(r"[A-Za-z_]\w*")

# The actionable classes — orphaned + test-only-reachable — are the consolidation/removal targets. The
# rest (unresolved suspects, framework-decorated, test-support hooks) are mostly live or expected noise.
_ACTIONABLE_MAX_RANK = ADVISORY_RANK[ADVISORY_TEST_ONLY]

DEAD_SYMBOL_RULE = RuleSpec(
    rule="reachability/dead-symbol",
    engine=ENGINE_REACHABILITY,
    default_severity=Severity.ERROR,
    category="Dead code",
    help="Top-level function/class with no runtime consumer (unreferenced, or referenced only by tests). "
    "Remove it or wire it into the product; downgrade via `.deadcode.yml` rules if it is intentional API.",
)

DUPLICATE_CONSTANT_RULE = RuleSpec(
    rule="reachability/duplicate-constant",
    engine=ENGINE_REACHABILITY,
    default_severity=Severity.WARNING,
    category="Duplicated rules",
    help="The same string constant is assigned at module level in several modules, so a business rule/token "
    "lives in two places. Define it once in the owning module and import it elsewhere.",
)

_MIN_CONSTANT_LENGTH = 10  # shorter strings ("utf-8", "__main__", "id") repeat by coincidence
_DOTTED_PATH = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+")  # "pkg.module.Name": an import path, not a rule


def build_candidates() -> list[PublicSymbolDefinition]:
    """Every public top-level symbol in ``src/`` — the whole-tree corpus (NOT diff-scoped)."""
    candidates: list[PublicSymbolDefinition] = []
    for path in diffscope.runtime_files():
        candidates.extend(public_symbol_definitions(path))
    return candidates


def _occurrence_index(corpus_text: dict[Path, str], names: set[str]) -> dict[str, set[tuple[Path, int]]]:
    """Map each name in ``names`` to the ``{(file, lineno)}`` where it appears as a whole word."""
    index: dict[str, set[tuple[Path, int]]] = {}
    for path, text in corpus_text.items():
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name in names.intersection(_IDENT.findall(line)):
                index.setdefault(name, set()).add((path, lineno))
    return index


def _test_reference_names(names: set[str]) -> set[str]:
    """Subset of ``names`` appearing as a whole word anywhere under ``tests/`` (untracked-aware, .py-only)."""
    found: set[str] = set()
    paths = {p for root in diffscope.project_paths("test_roots", [Path("tests")])
             for p in (diffscope.REPO_ROOT / root).rglob("*.py")}
    for path in sorted(paths):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            found |= names.intersection(_IDENT.findall(line))
            if found == names:
                return found  # every candidate already seen — no need to read more test files
    return found


def scan() -> list[tuple[PublicSymbolDefinition, str]]:
    """Return every src public symbol with no RESOLVED consumer, paired with its advisory class, ranked."""
    candidates = build_candidates()

    # Read the runtime corpus once; reuse the text for BOTH the resolver facts and the occurrence index.
    corpus_text = {path: diffscope.read_text(path) for path in runtime_corpus_files()}
    facts = [collect_module_facts(text, path) for path, text in corpus_text.items()]
    reexport_map = build_reexport_map(facts)
    entry_points = entry_point_targets()
    whitelisted = vulture_whitelist_names()

    unresolved: list[PublicSymbolDefinition] = []
    for definition in candidates:
        if definition.symbol in whitelisted:
            continue  # the repo's configured Vulture whitelist declares it used (same contract as the Vulture gate)
        dotted = module_dotted_name(definition.file_path)
        if (dotted, definition.symbol) in entry_points:
            continue  # consumed by a packaging entry point ([project.scripts] etc.)
        if dotted is not None and resolved_reference_exists(definition.symbol, dotted, facts, reexport_map):
            continue  # consumed by a resolved reference
        unresolved.append(definition)

    names = {definition.symbol for definition in unresolved}
    runtime_locations = _occurrence_index(corpus_text, names)
    test_reference_names = _test_reference_names(names)

    rows: list[tuple[PublicSymbolDefinition, str]] = []
    for definition in unresolved:
        own = (definition.file_path, definition.line)
        occurs_in_runtime = any(location != own for location in runtime_locations.get(definition.symbol, set()))
        _verdict, reason = classify_unresolved(
            definition,
            occurs_in_runtime=occurs_in_runtime,
            has_test_ref=definition.symbol in test_reference_names,
            orphaned_blocks=False,  # whole-tree scan never blocks; orphaned -> top advisory instead
        )
        if reason is None:  # orphaned_blocks=False => every verdict is advisory
            raise RuntimeError("advisory verdict without a reason")
        rows.append((definition, reason))

    rows.sort(
        key=lambda row: (
            ADVISORY_RANK.get(row[1], 99),
            row[0].file_path.as_posix(),
            row[0].line,
        )
    )
    return rows


def dead_symbol_diagnostics(files: list[Path]) -> list[Diagnostic]:
    """Actionable whole-tree rows (orphaned + test-only-reachable) defined in ``files``, as gate diagnostics."""
    try:
        rows = scan()
    except SyntaxError:
        return []  # the per-file pass already reports deadcode/parse-error, which fails the gate
    scanned = set(files)
    return [
        diagnostic_from_spec(
            DEAD_SYMBOL_RULE,
            file_path=definition.file_path.as_posix(),
            line=definition.line,
            message=f"{definition.symbol}: {reason}",
        )
        for definition, reason in rows
        if definition.file_path in scanned and ADVISORY_RANK.get(reason, 99) <= _ACTIONABLE_MAX_RANK
    ]


def _string_constants(source: str) -> list[tuple[str, str, int]]:
    """``(name, value, line)`` of module-level ``NAME = "<str>"`` constants worth comparing across modules."""
    tree = safe_parse(source)
    if tree is None:
        return []
    exported = {
        elt.value
        for node in tree.body
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))
        for elt in node.value.elts
        if isinstance(elt, ast.Constant)
    }
    found: list[tuple[str, str, int]] = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        value = getattr(node, "value", None)
        if len(targets) != 1 or not isinstance(targets[0], ast.Name) or not isinstance(value, ast.Constant):
            continue
        name, text = targets[0].id, value.value
        if (
            isinstance(text, str)
            and len(text) >= _MIN_CONSTANT_LENGTH
            and not _DOTTED_PATH.fullmatch(text)
            and not (name.startswith("__") and name.endswith("__"))
            and name not in exported
        ):
            found.append((name, text, node.lineno))
    return found


def _owner_module(sites: list[tuple[Path, str, int]], corpus_text: dict[Path, str]) -> Path | None:
    """The duplicated constant's natural home: the module the other duplicating modules (transitively) import.

    Ties break on how many modules import it directly; a still-tied top is undecidable -> None (the message
    then just lists the locations).
    """
    facts = [collect_module_facts(text, path) for path, text in corpus_text.items()]
    known = {fact.dotted for fact in facts}
    edges: dict[str, set[str]] = defaultdict(set)
    for fact in facts:
        for module, name, _alias in fact.from_imports:
            edges[fact.dotted] |= {m for m in (module, f"{module}.{name}") if m in known}
        edges[fact.dotted] |= {module for module, _alias in fact.plain_imports if module in known}

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        todo = [start]
        while todo:
            todo.extend(edges[todo.pop()] - seen - {start})
            seen |= set(todo)
        return seen

    group = {module_dotted_name(path): path for path, _name, _line in sites}
    reach = {dotted: reachable(dotted) for dotted in group}
    ranked = sorted(
        (
            (
                sum(dotted in reach[other] for other in group if other != dotted),
                sum(dotted in targets for targets in edges.values()),
            ),
            path,
        )
        for dotted, path in group.items()
    )
    best = ranked[-1][0]
    return ranked[-1][1] if [key for key, _path in ranked].count(best) == 1 else None


def duplicate_constant_diagnostics(files: list[Path]) -> list[Diagnostic]:
    """One warning per ``files`` site of a long string constant assigned in 2+ runtime modules."""
    corpus_text = {path: diffscope.read_text(path) for path in runtime_corpus_files()}
    by_value: dict[str, list[tuple[Path, str, int]]] = defaultdict(list)
    for path, text in corpus_text.items():
        for name, value, line in _string_constants(text):
            by_value[value].append((path, name, line))
    scanned = set(files)
    diagnostics: list[Diagnostic] = []
    for value, sites in by_value.items():
        if len({path for path, _name, _line in sites}) < 2:
            continue  # repeated inside ONE module is that module's own business
        owner = _owner_module(sites, corpus_text)
        shown = value if len(value) <= 40 else value[:37] + "..."
        for path, name, line in sites:
            if path == owner or path not in scanned:
                continue
            others = ", ".join(f"{p.as_posix()}:{n}" for p, _name, n in sites if (p, n) != (path, line))
            home = f"; define it once in {owner.as_posix()} and import it" if owner else ""
            diagnostics.append(
                diagnostic_from_spec(
                    DUPLICATE_CONSTANT_RULE,
                    file_path=path.as_posix(),
                    line=line,
                    message=f'{name} = "{shown}" is also assigned at {others}{home}',
                )
            )
    return sorted(diagnostics, key=lambda d: (d.file_path, d.line))


def _shown_rows(rows: list[tuple[PublicSymbolDefinition, str]], top: int) -> list[tuple[PublicSymbolDefinition, str]]:
    """The detail rows to print: actionable classes ALWAYS in full; lower-confidence noise capped at ``top``.

    Truncation keys off the confidence RANK, never a fixed count, so the actionable orphaned + test-only
    rows are never hidden no matter how many there are (``top`` only bounds the decorated/unresolved noise).
    """
    if top <= 0:
        return rows
    actionable = [row for row in rows if ADVISORY_RANK.get(row[1], 99) <= _ACTIONABLE_MAX_RANK]
    noise = [row for row in rows if ADVISORY_RANK.get(row[1], 99) > _ACTIONABLE_MAX_RANK]
    return actionable + noise[:top]


def _class_summary(rows: list[tuple[PublicSymbolDefinition, str]]) -> list[str]:
    """Per-class count lines over the FULL row set, highest-confidence class first."""
    from collections import Counter

    by_class: Counter[str] = Counter(reason for _definition, reason in rows)
    return [
        f"  [{by_class[reason]:>3}] {ADVISORY_LABEL.get(reason, reason)}"
        for reason in sorted(by_class, key=lambda reason: ADVISORY_RANK.get(reason, 99))
    ]


def _render_human(rows: list[tuple[PublicSymbolDefinition, str]], *, top: int) -> str:
    lines = [f"reachability scan: {len(rows)} src public symbol(s) with no RESOLVED consumer (whole-tree, advisory):"]
    lines.extend(_class_summary(rows))
    shown = _shown_rows(rows, top)
    for definition, reason in shown:
        lines.append(f"  {definition.file_path.as_posix()}:{definition.line} {definition.symbol} -> {reason}")
    hidden = len(rows) - len(shown)
    if hidden:
        lines.append(f"  ... {hidden} lower-confidence row(s) hidden (--top 0 for all)")
    lines.append(
        "  -> orphaned + test-only-reachable are the actionable groups (remove or wire); decorated is usually live"
    )
    return "\n".join(lines)


def _render_json(rows: list[tuple[PublicSymbolDefinition, str]]) -> str:
    """JSON is ALWAYS the complete list (``--top`` only pages the human view)."""
    payload = {
        "count": len(rows),
        "symbols": [
            {
                "file": definition.file_path.as_posix(),
                "line": definition.line,
                "symbol": definition.symbol,
                "decorated": definition.decorated,
                "reason": reason,
            }
            for definition, reason in rows
        ],
    }
    return json.dumps(payload, indent=2)


def run(*, top: int, as_json: bool) -> int:
    """Build the whole-tree corpus, classify every unresolved public symbol, render, ALWAYS return 0."""
    rows = scan()
    print(_render_json(rows) if as_json else _render_human(rows, top=top))
    return 0
