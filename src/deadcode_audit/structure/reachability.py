"""Tier 4: runtime-consumer dead-code gate for changed public symbols.

A changed top-level PUBLIC symbol is reported when no runtime code consumes it. Two layers:

* **Blocking floor (conservative, unchanged from the historical gate).** A symbol is
  *hard-blocked* only when a word-boundary ``git grep`` finds its name NOWHERE in runtime
  code (``src`` + ``scripts``), including strings/comments, AND it is undecorated. Because a
  real consumer — even a dynamic ``getattr("name")`` / string-registry one — leaves the name
  somewhere, this floor never blocks live code (it is false-negative-biased by construction).

* **Resolver refinement (advisory only).** On top of the floor, an AST import-resolver checks
  whether each symbol's name actually RESOLVES to a real reference (an import of it from its
  module or a re-exporting package, a qualified ``module.symbol`` attribute access, an
  intra-module use, or a star import). A symbol whose name *occurs* (so the floor does not
  block it) but does NOT resolve is surfaced as ADVISORY — the same-name-collision / dynamic
  -dispatch suspects the bare grep used to pass silently. Decorated symbols are never
  hard-blocked (framework registration is invisible to static analysis); when unresolved they
  are advisory.

Why grep stays the blocking authority: a static resolver cannot see re-exports, aliased
attribute chains, ``getattr``/string registries, or ``importlib`` plugin loading without
false-positively blocking live code. Keeping grep as the floor means resolver incompleteness
only ever produces advisory NOISE, never a blocked-live-code regression. (See the
``aislop-detector-loop`` skill for the full rationale and the gate that drove this design.)
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from deadcode_audit import diffscope, layout
from deadcode_audit.framework import safe_parse
from deadcode_audit.structure.resolver import (
    build_reexport_map,
    collect_module_facts,
    entry_point_targets,
    module_dotted_name,
    resolved_reference_exists,
    runtime_corpus_files,
)

# Advisory confidence classes, ordered most- to least-likely-actually-dead (the Vulture-confidence /
# SonarQube-severity model applied to reachability). A public symbol with no resolved consumer is:
# ORPHANED (name appears NOWHERE in runtime or tests — the strongest dead signal; in the diff-scoped
# gate this is the hard-BLOCK condition, in the whole-tree scan it is the top advisory) > TEST_ONLY
# (referenced only by tests: an unwired / speculative API) > UNRESOLVED (name occurs but does not
# statically resolve — collision/dynamic suspect) > DECORATED (framework-registered, usually live) >
# TEST_SUPPORT (name follows the ``*_for_tests`` "testonly" convention — an intentional hook, not dead).
# Decoration takes precedence over orphaned: a decorator the resolver cannot see is framework wiring,
# not dead code. The reason string is the SSOT — the rank/label maps key off it so output ordering and
# the summary stay in sync.
_ADVISORY_ORPHANED = "no runtime or test reference anywhere (strongest dead candidate)"
ADVISORY_TEST_ONLY = "no runtime consumer; referenced only by tests (unwired / speculative API)"
_ADVISORY_UNRESOLVED = "name occurs but does not resolve to a reference (same-name collision or dynamic use)"
_ADVISORY_DECORATED = "decorated; may be framework-registered or dynamically invoked"
_ADVISORY_TEST_SUPPORT = "no runtime consumer; named *_for_tests (test-support hook — expected, not dead)"

# Suffixes that mark an intentional test-support hook by convention (Google's ``testonly`` /
# pytest fixture-helper idiom). Matched on the symbol name only — a class-level invariant, not a value.
_TEST_SUPPORT_SUFFIXES = ("_for_tests", "_for_test", "_for_testing")

ADVISORY_RANK = {
    _ADVISORY_ORPHANED: 0,
    ADVISORY_TEST_ONLY: 1,
    _ADVISORY_UNRESOLVED: 2,
    _ADVISORY_DECORATED: 3,
    _ADVISORY_TEST_SUPPORT: 4,
}
ADVISORY_LABEL = {
    _ADVISORY_ORPHANED: "orphaned             (no runtime/test reference anywhere — strongest dead candidate)",
    ADVISORY_TEST_ONLY: "test-only-reachable  (product-dead candidates — remove or wire)",
    _ADVISORY_UNRESOLVED: "name unresolved      (same-name collision / dynamic-dispatch suspects)",
    _ADVISORY_DECORATED: "decorated            (framework-registered; usually live, verify if unsure)",
    _ADVISORY_TEST_SUPPORT: "test-support hook    (named *_for_tests; intentional, not dead)",
}


def _is_test_support_name(symbol: str) -> bool:
    """True when the symbol name follows the ``*_for_tests`` test-support convention."""
    return symbol.endswith(_TEST_SUPPORT_SUFFIXES)


@dataclass(frozen=True)
class PublicSymbolDefinition:
    """Describe a top-level public symbol in a Python module."""

    file_path: Path
    symbol: str
    line: int
    decorated: bool = False


def public_symbol_definitions(path: Path) -> list[PublicSymbolDefinition]:
    """Return top-level function and class definitions (public AND private; dunders skipped) for a module.

    Private ``_name`` symbols are candidates too: an unreferenced private helper is the most obviously
    dead code there is, and the resolver already counts an intra-module use as a consumer.
    Decorated symbols are INCLUDED (with ``decorated=True``) — unlike the old skip-all — so
    decorated-but-dead symbols can still be surfaced. The decorator flag only downgrades them
    from blocking to advisory later; it never grants a silent pass.
    """
    # utf-8-sig: tolerate UTF-8-BOM files (U+FEFF) in target corpora, which crash ast.parse.
    source = (layout.REPO_ROOT / path).read_text(encoding="utf-8-sig")
    tree = safe_parse(source)
    if tree is None:
        # An unparseable file must not crash the gate with a traceback. Warn and continue: the
        # blocking floor is raw-text grep, which still sees the file's names, so skipping the
        # resolver for this file can only cost advisory coverage, never hide a live consumer.
        print(f"warning: {path.as_posix()} does not parse; skipping its symbol definitions", file=sys.stderr)
        return []
    definitions: list[PublicSymbolDefinition] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("__") and node.name.endswith("__"):
                continue  # module dunders (``__getattr__``/``__dir__``) are invoked by the interpreter
            definitions.append(
                PublicSymbolDefinition(
                    file_path=path,
                    symbol=node.name,
                    line=node.lineno,
                    decorated=bool(node.decorator_list),
                )
            )
    return definitions


def _runtime_reference_locations(symbol: str) -> list[tuple[Path, int]]:
    """Return runtime source locations where the symbol's name appears (word-boundary grep)."""
    lines = diffscope.git_lines("grep", "-n", "-w", "-e", symbol, "--", *(p.as_posix() for p in layout.source_roots()))
    locations: list[tuple[Path, int]] = []
    for entry in lines:
        path_text, line_text, _ = entry.split(":", 2)
        path = Path(path_text)
        if not layout.is_runtime_python_path(path):
            continue
        locations.append((path, int(line_text)))
    return locations


def _has_test_reference(symbol: str) -> bool:
    """True when the symbol's name appears anywhere under ``tests`` (word-boundary grep).

    A symbol referenced only by tests has no RUNTIME consumer but is not *orphaned* — it may be a
    test-support helper (``reset_for_tests``) or a built-but-unwired API that tests cover. Either
    way it must NOT be hard-blocked (blocking a referenced symbol is a false positive); it is
    surfaced as advisory instead.
    """
    completed = subprocess.run(
        ["git", "grep", "-l", "-w", "-e", symbol, "--", *(p.as_posix() for p in layout.project_paths("test_roots", [Path("tests")]))],
        cwd=layout.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # git grep exit codes: 0 = matches found, 1 = no matches (a normal result, not an error),
    # anything else = a real git failure that must surface (fail-fast, not swallowed).
    if completed.returncode == 1:
        return False
    if completed.returncode != 0:
        raise RuntimeError(f"git grep failed (exit {completed.returncode}): {completed.stderr.strip()}")
    return bool(completed.stdout.strip())


def symbols_without_runtime_consumers(
    definitions: list[PublicSymbolDefinition],
    reference_locations_by_symbol: dict[str, list[tuple[Path, int]]],
) -> list[PublicSymbolDefinition]:
    """Return public symbols whose name appears at NO runtime location beyond their definition.

    This is the conservative blocking floor: it uses the word-boundary grep references, so a
    symbol is only here when its name is absent everywhere else (no caller, no string, no
    comment) — the historical, false-negative-biased dead-code condition.
    """
    missing: list[PublicSymbolDefinition] = []
    for definition in definitions:
        locations = reference_locations_by_symbol.get(definition.symbol, [])
        has_runtime_consumer = any(
            location_path != definition.file_path or location_line != definition.line
            for location_path, location_line in locations
        )
        if not has_runtime_consumer:
            missing.append(definition)
    return missing


def classify_unresolved(
    definition: PublicSymbolDefinition,
    *,
    occurs_in_runtime: bool,
    has_test_ref: bool,
    orphaned_blocks: bool,
) -> tuple[str, str | None]:
    """Map one symbol with no RESOLVED consumer to a verdict — shared by the gate and the whole-tree scan.

    Pure (no I/O): the caller supplies the already-computed facts (``occurs_in_runtime`` = the name appears
    in runtime code somewhere other than its own definition; ``has_test_ref`` = the name appears under
    ``tests``). Returns ``("block", None)`` ONLY when ``orphaned_blocks`` and the symbol is truly orphaned
    (no runtime ref, no test ref, undecorated) — the diff-scoped gate's hard block. Otherwise returns
    ``("advisory", reason)`` with the confidence class. The whole-tree scan passes ``orphaned_blocks=False``
    so a truly-orphaned symbol becomes the highest-confidence advisory (:data:`_ADVISORY_ORPHANED`) rather
    than blocking. **Decoration takes precedence over orphaned**: a decorated symbol is framework-registered
    (:data:`_ADVISORY_DECORATED`), never orphaned — the decorator the resolver cannot see is wiring, not death.
    """
    if not occurs_in_runtime and not definition.decorated:
        if has_test_ref:
            if _is_test_support_name(definition.symbol):
                return ("advisory", _ADVISORY_TEST_SUPPORT)
            return ("advisory", ADVISORY_TEST_ONLY)
        if orphaned_blocks:
            return ("block", None)
        return ("advisory", _ADVISORY_ORPHANED)
    if definition.decorated:
        return ("advisory", _ADVISORY_DECORATED)
    return ("advisory", _ADVISORY_UNRESOLVED)


def runtime_consumer_check(paths: list[Path]) -> int:
    """Block on changed public symbols with no consumer; advise on collision/dynamic/decorated suspects."""
    changed_runtime_paths = [path for path in paths if layout.is_runtime_python_path(path)]
    if not changed_runtime_paths:
        print("No changed src runtime files for consumer check")
        return 0

    definitions: list[PublicSymbolDefinition] = []
    for path in changed_runtime_paths:
        definitions.extend(public_symbol_definitions(path))
    if not definitions:
        print("No changed public symbols for consumer check")
        return 0

    # Blocking floor: name appears nowhere else in runtime code (incl. strings/comments).
    symbols = sorted({definition.symbol for definition in definitions})
    grep_references = {symbol: _runtime_reference_locations(symbol) for symbol in symbols}
    no_grep_consumer = {
        (d.file_path, d.line, d.symbol) for d in symbols_without_runtime_consumers(definitions, grep_references)
    }

    # Resolver refinement: does each symbol's name resolve to a real reference?
    facts = [collect_module_facts(layout.read_text(p), p) for p in runtime_corpus_files()]
    reexport_map = build_reexport_map(facts)
    entry_points = entry_point_targets()

    blocking: list[PublicSymbolDefinition] = []
    advisory: list[tuple[PublicSymbolDefinition, str]] = []
    for definition in definitions:
        def_dotted = module_dotted_name(definition.file_path)
        if (def_dotted, definition.symbol) in entry_points:
            continue  # consumed by a packaging entry point
        if def_dotted is not None and resolved_reference_exists(definition.symbol, def_dotted, facts, reexport_map):
            continue  # consumed
        key = (definition.file_path, definition.line, definition.symbol)
        occurs_in_runtime = key not in no_grep_consumer
        # _has_test_reference is one git grep; compute it only in the branch the classifier reads it
        # (orphaned candidates), so the gate makes no more subprocess calls than before this refactor.
        has_test_ref = (
            _has_test_reference(definition.symbol) if (not occurs_in_runtime and not definition.decorated) else False
        )
        verdict, reason = classify_unresolved(
            definition,
            occurs_in_runtime=occurs_in_runtime,
            has_test_ref=has_test_ref,
            orphaned_blocks=True,  # diff-scoped gate: a truly-orphaned changed symbol hard-blocks
        )
        if verdict == "block":
            blocking.append(definition)  # referenced nowhere in src/scripts/tests == truly dead
        else:
            if reason is None:  # advisory verdicts always carry a reason (an assert would vanish under -O)
                raise RuntimeError(f"advisory verdict without a reason for {definition.symbol}")
            advisory.append((definition, reason))

    if advisory:
        # Rank by deadness confidence (highest first), then file/line, so the actionable
        # product-dead candidates surface at the top instead of being buried under framework noise.
        advisory.sort(
            key=lambda item: (
                ADVISORY_RANK.get(item[1], 99),
                item[0].file_path.as_posix(),
                item[0].line,
            )
        )
        by_class = Counter(reason for _definition, reason in advisory)
        print(f"[advisory, non-blocking] {len(advisory)} changed public symbol(s) with no RESOLVED consumer:")
        for reason in sorted(by_class, key=lambda r: ADVISORY_RANK.get(r, 99)):
            print(f"  [{by_class[reason]:>3}] {ADVISORY_LABEL.get(reason, reason)}")
        for definition, reason in advisory:
            print(f"  {definition.file_path.as_posix()}:{definition.line} {definition.symbol} -> {reason}")
        print("  -> ranked by deadness confidence; the test-only-reachable group is the one to act on first")

    if blocking:
        print("Runtime consumer check failed (no consumer anywhere for these public symbols):")
        for definition in blocking:
            print(f"{definition.file_path.as_posix()}:{definition.line} {definition.symbol}")
        return 1

    print("Runtime consumer check passed")
    return 0
