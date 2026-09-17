"""On-demand, advisory WHOLE-TREE dead-symbol reachability scan (beyond the diff-scoped Tier 4 gate).

Where :func:`deadcode_audit.reachability.runtime_consumer_check` checks only the CHANGED public symbols
and BLOCKS truly-orphaned ones, this scans EVERY public top-level symbol in ``src/`` and reports the ones
with no resolved runtime consumer, ranked by deadness confidence. Like the ``overlaps`` subcommand it is
on-demand and advisory: :func:`run` ALWAYS returns 0 and it is deliberately NOT part of ``make deadcode`` /
pre-commit / CI.

It reuses the Tier 4 resolver and the SHARED :func:`deadcode_audit.reachability.classify_unresolved`
classifier, so every symbol lands in the SAME confidence classes the gate uses (plus the undecorated-orphan
top tier). The occurrence floor is computed DIFFERENTLY from the gate on purpose: the gate's ``git grep``
floor sees only TRACKED files and counts non-``.py`` doc hits — fine commit-time/diff-scoped, but wrong for a
whole-tree scan run mid-development (the whole ``scripts/deadcode`` package is untracked as it is built, and
a README mention is not a runtime consumer). So the scan builds an in-memory occurrence index over the
``.py`` corpus it already reads for the resolver: untracked-aware (``rglob``, not ``git grep``), ``.py``-only,
and zero subprocess.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from deadcode_audit import diffscope
from deadcode_audit.reachability import (
    ADVISORY_LABEL,
    ADVISORY_RANK,
    ADVISORY_TEST_ONLY,
    PublicSymbolDefinition,
    build_reexport_map,
    classify_unresolved,
    collect_module_facts,
    module_dotted_name,
    public_symbol_definitions,
    resolved_reference_exists,
    runtime_corpus_files,
)


# Whole-word identifier tokenizer == ``git grep -w`` semantics: a name "occurs" only as a complete
# token (so ``reset_for_tests`` does NOT match inside ``_reset_for_tests``). Run over RAW lines, it also
# sees names in strings/comments — exactly as the grep floor did — so dynamic ``getattr("name")`` /
# string-registry use still demotes a symbol out of the orphaned tier.
_IDENT = re.compile(r"[A-Za-z_]\w*")

# The actionable classes — orphaned + test-only-reachable — are the consolidation/removal targets. The
# rest (unresolved suspects, framework-decorated, test-support hooks) are mostly live or expected noise.
_ACTIONABLE_MAX_RANK = ADVISORY_RANK[ADVISORY_TEST_ONLY]


def build_candidates() -> list[PublicSymbolDefinition]:
    """Every public top-level symbol in ``src/`` — the whole-tree corpus (NOT diff-scoped)."""
    candidates: list[PublicSymbolDefinition] = []
    for path in diffscope.all_src_files():
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

    unresolved: list[PublicSymbolDefinition] = []
    for definition in candidates:
        dotted = module_dotted_name(definition.file_path)
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
        assert reason is not None  # orphaned_blocks=False => every verdict is advisory
        rows.append((definition, reason))

    rows.sort(
        key=lambda row: (
            ADVISORY_RANK.get(row[1], 99),
            row[0].file_path.as_posix(),
            row[0].line,
        )
    )
    return rows


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
