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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from deadcode_audit import diffscope

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
_ADVISORY_TEST_ONLY = "no runtime consumer; referenced only by tests (unwired / speculative API)"
_ADVISORY_UNRESOLVED = "name occurs but does not resolve to a reference (same-name collision or dynamic use)"
_ADVISORY_DECORATED = "decorated; may be framework-registered or dynamically invoked"
_ADVISORY_TEST_SUPPORT = "no runtime consumer; named *_for_tests (test-support hook — expected, not dead)"

# Suffixes that mark an intentional test-support hook by convention (Google's ``testonly`` /
# pytest fixture-helper idiom). Matched on the symbol name only — a class-level invariant, not a value.
_TEST_SUPPORT_SUFFIXES = ("_for_tests", "_for_test", "_for_testing")

_ADVISORY_RANK = {
    _ADVISORY_ORPHANED: 0,
    _ADVISORY_TEST_ONLY: 1,
    _ADVISORY_UNRESOLVED: 2,
    _ADVISORY_DECORATED: 3,
    _ADVISORY_TEST_SUPPORT: 4,
}
_ADVISORY_LABEL = {
    _ADVISORY_ORPHANED: "orphaned             (no runtime/test reference anywhere — strongest dead candidate)",
    _ADVISORY_TEST_ONLY: "test-only-reachable  (product-dead candidates — remove or wire)",
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


def _public_symbol_definitions(path: Path) -> list[PublicSymbolDefinition]:
    """Return top-level public function and class definitions for a module.

    Decorated symbols are INCLUDED (with ``decorated=True``) — unlike the old skip-all — so
    decorated-but-dead symbols can still be surfaced. The decorator flag only downgrades them
    from blocking to advisory later; it never grants a silent pass.
    """
    source = (diffscope.REPO_ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions: list[PublicSymbolDefinition] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
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
    lines = diffscope._git_lines("grep", "-n", "-w", "-e", symbol, "--", *(p.as_posix() for p in diffscope.source_roots()))
    locations: list[tuple[Path, int]] = []
    for entry in lines:
        path_text, line_text, _ = entry.split(":", 2)
        path = Path(path_text)
        if not diffscope.is_runtime_python_path(path):
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
        ["git", "grep", "-l", "-w", "-e", symbol, "--", *(p.as_posix() for p in diffscope.project_paths("test_roots", [Path("tests")]))],
        cwd=diffscope.REPO_ROOT,
        capture_output=True,
        text=True,
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


# --- AST import-resolver (advisory refinement only — never the blocking authority) ---


def module_dotted_name(path: Path) -> str | None:
    """Map a repo-relative runtime file to the dotted module name imports actually use.

    ``src`` is the source root (``src/modules/core/config.py`` -> ``modules.core.config``);
    ``scripts`` is its own package (``scripts/deadcode/diffscope.py`` -> ``deadcode_audit.diffscope``).
    ``__init__.py`` maps to its package. Returns None for non-runtime paths.
    """
    roots = diffscope.project_paths("import_roots", [Path("src"), Path(".")])
    matching = [root for root in roots if path.is_relative_to(root)]
    if not matching or not diffscope.is_runtime_python_path(path):
        return None
    root = max(matching, key=lambda p: len(p.parts))
    rel = path.relative_to(root).as_posix()
    if not rel.endswith(".py"):
        return None
    rel = rel[: -len(".py")]
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


@dataclass(frozen=True)
class ModuleFacts:
    """Import edges and name/attribute uses extracted from one runtime module."""

    dotted: str
    is_package: bool
    from_imports: tuple[tuple[str, str, str], ...] = ()  # (module, name, asname)
    star_from: frozenset[str] = frozenset()  # ``from X import *`` -> {X}
    plain_imports: tuple[tuple[str, str], ...] = ()  # (module, asname)
    used_names: frozenset[str] = frozenset()  # ast.Name (Load)
    used_attrs: frozenset[tuple[str, str]] = frozenset()  # (base_name, attr)
    dunder_all: frozenset[str] = frozenset()
    public_defs: frozenset[str] = field(default=frozenset())


def _resolve_relative(dotted: str, is_package: bool, level: int, module: str | None) -> str | None:
    """Resolve a relative import target to an absolute dotted module name."""
    if level == 0:
        return module
    package = dotted if is_package else (dotted.rsplit(".", 1)[0] if "." in dotted else "")
    parts = package.split(".") if package else []
    drop = level - 1
    if drop > len(parts):
        return None
    base = parts[: len(parts) - drop] if drop else parts
    base_dotted = ".".join(base)
    if module:
        return f"{base_dotted}.{module}" if base_dotted else module
    return base_dotted or None


def collect_module_facts(source: str, path: Path) -> ModuleFacts:
    """Extract import edges + name/attribute uses + ``__all__`` + public defs from a module."""
    dotted = module_dotted_name(path)
    is_package = path.name == "__init__.py"
    tree = ast.parse(source)

    from_imports: list[tuple[str, str, str]] = []
    star_from: set[str] = set()
    plain_imports: list[tuple[str, str]] = []
    used_names: set[str] = set()
    used_attrs: set[tuple[str, str]] = set()
    dunder_all: set[str] = set()
    public_defs: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            public_defs.add(node.name)
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))
        ):
            dunder_all.update(
                elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            )

    for sub in ast.walk(tree):
        if isinstance(sub, ast.ImportFrom):
            target = _resolve_relative(dotted or "", is_package, sub.level, sub.module)
            if target is None:
                continue
            for alias in sub.names:
                if alias.name == "*":
                    star_from.add(target)
                else:
                    from_imports.append((target, alias.name, alias.asname or alias.name))
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                plain_imports.append((alias.name, alias.asname or alias.name.split(".")[0]))
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            used_names.add(sub.id)
        elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
            used_attrs.add((sub.value.id, sub.attr))

    return ModuleFacts(
        dotted=dotted or "",
        is_package=is_package,
        from_imports=tuple(from_imports),
        star_from=frozenset(star_from),
        plain_imports=tuple(plain_imports),
        used_names=frozenset(used_names),
        used_attrs=frozenset(used_attrs),
        dunder_all=frozenset(dunder_all),
        public_defs=frozenset(public_defs),
    )


def build_reexport_map(facts: list[ModuleFacts]) -> dict[str, set[str]]:
    """Map a symbol name -> set of package modules that re-export it via their ``__init__``.

    A package ``__init__`` that does ``from .sub import S`` (optionally listing S in
    ``__all__``) re-exposes S under the package's dotted name, so ``from package import S`` in a
    consumer resolves to the original definition.
    """
    reexport: dict[str, set[str]] = {}
    for fact in facts:
        if not fact.is_package:
            continue
        for _module, name, asname in fact.from_imports:
            exposed = asname
            reexport.setdefault(exposed, set()).add(fact.dotted)
    return reexport


def _aliases_for_module(fact: ModuleFacts, def_dotted: str) -> set[str]:
    """Local names in ``fact`` bound to the defining module (for ``module.symbol`` access)."""
    leaf = def_dotted.rsplit(".", 1)[-1]
    parent = def_dotted.rsplit(".", 1)[0] if "." in def_dotted else ""
    aliases: set[str] = set()
    for module, asname in fact.plain_imports:
        if module == def_dotted:
            aliases.add(asname)
    for module, name, asname in fact.from_imports:
        # ``from <parent> import <leaf>`` then ``<leaf>.symbol``
        if parent and module == parent and name == leaf:
            aliases.add(asname)
    return aliases


def resolved_reference_exists(
    symbol: str,
    def_dotted: str,
    facts: list[ModuleFacts],
    reexport_map: dict[str, set[str]],
) -> bool:
    """True when some runtime module holds a RESOLVED reference to ``symbol`` defined in ``def_dotted``.

    Covers: intra-module use, direct/aliased ``from <def_module> import symbol`` (and re-export
    packages), qualified ``module.symbol`` attribute access, and ``from <def_module> import *``.
    Best-effort and biased toward "resolved" (a miss only yields an advisory, never a block).
    """
    exposing = {def_dotted} | reexport_map.get(symbol, set())
    for fact in facts:
        if fact.dotted == def_dotted:
            # Intra-module reference (use of the name somewhere other than the definition itself).
            if symbol in fact.used_names or any(attr == symbol for _base, attr in fact.used_attrs):
                return True
            continue
        # Direct / re-exported from-import of the symbol.
        for module, name, _asname in fact.from_imports:
            if name == symbol and module in exposing:
                return True
        # Qualified ``module.symbol`` attribute access.
        if any((alias, symbol) in fact.used_attrs for alias in _aliases_for_module(fact, def_dotted)):
            return True
        # Star import from the defining module that then uses the bare name.
        if def_dotted in fact.star_from and symbol in fact.used_names:
            return True
    return False


def _runtime_corpus_files() -> list[Path]:
    """Every runtime ``src`` + ``scripts`` Python file, repo-relative (the resolver corpus)."""
    return diffscope.runtime_files()


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
            return ("advisory", _ADVISORY_TEST_ONLY)
        if orphaned_blocks:
            return ("block", None)
        return ("advisory", _ADVISORY_ORPHANED)
    if definition.decorated:
        return ("advisory", _ADVISORY_DECORATED)
    return ("advisory", _ADVISORY_UNRESOLVED)


def runtime_consumer_check(paths: list[Path]) -> int:
    """Block on changed public symbols with no consumer; advise on collision/dynamic/decorated suspects."""
    changed_runtime_paths = [path for path in paths if diffscope.is_runtime_python_path(path)]
    if not changed_runtime_paths:
        print("No changed src runtime files for consumer check")
        return 0

    definitions: list[PublicSymbolDefinition] = []
    for path in changed_runtime_paths:
        definitions.extend(_public_symbol_definitions(path))
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
    facts = [collect_module_facts(diffscope.read_text(p), p) for p in _runtime_corpus_files()]
    reexport_map = build_reexport_map(facts)

    blocking: list[PublicSymbolDefinition] = []
    advisory: list[tuple[PublicSymbolDefinition, str]] = []
    for definition in definitions:
        def_dotted = module_dotted_name(definition.file_path)
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
            assert reason is not None  # advisory verdicts always carry a reason
            advisory.append((definition, reason))

    if advisory:
        # Rank by deadness confidence (highest first), then file/line, so the actionable
        # product-dead candidates surface at the top instead of being buried under framework noise.
        advisory.sort(
            key=lambda item: (
                _ADVISORY_RANK.get(item[1], 99),
                item[0].file_path.as_posix(),
                item[0].line,
            )
        )
        by_class = Counter(reason for _definition, reason in advisory)
        print(f"[advisory, non-blocking] {len(advisory)} changed public symbol(s) with no RESOLVED consumer:")
        for reason in sorted(by_class, key=lambda r: _ADVISORY_RANK.get(r, 99)):
            print(f"  [{by_class[reason]:>3}] {_ADVISORY_LABEL.get(reason, reason)}")
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
