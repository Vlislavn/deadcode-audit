"""AST import-resolver behind the Tier 4 gate, the whole-tree reachability scan, the cycle detector and the architecture rules.

Pure, advisory-grade machinery: it extracts import edges and name uses per runtime module (:class:`ModuleFacts`),
follows re-exports, and answers "does this name resolve to a real reference?". It is never the blocking
authority of a gate (see :mod:`deadcode_audit.structure.reachability`).
"""

from __future__ import annotations

import ast
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from deadcode_audit import layout
from deadcode_audit.framework import safe_parse


def module_dotted_name(path: Path) -> str | None:
    """Map a repo-relative runtime file to the dotted module name imports actually use.

    ``src`` is the source root (``src/modules/core/config.py`` -> ``modules.core.config``);
    ``scripts`` is its own package (``scripts/deadcode/diffscope.py`` -> ``deadcode_audit.diffscope``).
    ``__init__.py`` maps to its package. Returns None for non-runtime paths.
    """
    roots = layout.project_paths("import_roots", [Path("src"), Path(".")])
    matching = [root for root in roots if path.is_relative_to(root)]
    if not matching or not layout.is_runtime_python_path(path):
        return None
    root = max(matching, key=lambda p: len(p.parts))
    rel = path.relative_to(root).as_posix()
    if not rel.endswith(".py"):
        return None
    rel = rel[: -len(".py")]
    rel = rel.removesuffix("/__init__")
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


def capped(items: list[str], limit: int) -> str:
    """Join ``items``, listing the first ``limit`` and ending with ``+N more``."""
    return ", ".join(items[:limit]) + (f", +{len(items) - limit} more" if len(items) > limit else "")


def collect_module_facts(source: str, path: Path, tree: ast.Module | None = None) -> ModuleFacts:
    """Extract import edges, name/attribute uses, ``__all__`` and public defs (reusing ``tree`` if given)."""
    dotted = module_dotted_name(path)
    is_package = path.name == "__init__.py"
    tree = tree or safe_parse(source)
    if tree is None:
        # Unparseable corpus file: warn and contribute no facts. Safe by construction — the
        # blocking floor is raw-text grep, not this resolver (a resolver miss only adds noise).
        print(f"warning: {path.as_posix()} does not parse; treating as no imports/uses", file=sys.stderr)
        return ModuleFacts(dotted=dotted or "", is_package=is_package)

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


def referencing_modules(
    symbol: str,
    def_dotted: str,
    facts: list[ModuleFacts],
    reexport_map: dict[str, set[str]],
) -> set[str]:
    """Dotted names of the runtime modules that hold a RESOLVED reference to ``symbol`` defined in ``def_dotted``.

    Covers: intra-module use (the defining module itself), direct/aliased ``from <def_module> import symbol``
    (and re-export packages), qualified ``module.symbol`` attribute access, and ``from <def_module> import *``.
    Best-effort and biased toward "resolved" (a miss only yields an advisory, never a block).
    """
    exposing = {def_dotted} | reexport_map.get(symbol, set())
    referencing: set[str] = set()
    for fact in facts:
        if fact.dotted == def_dotted:
            # Intra-module reference (use of the name somewhere other than the definition itself).
            if symbol in fact.used_names or any(attr == symbol for _base, attr in fact.used_attrs):
                referencing.add(fact.dotted)
            continue
        if (
            # Direct / re-exported from-import of the symbol.
            any(name == symbol and module in exposing for module, name, _asname in fact.from_imports)
            # Qualified ``module.symbol`` attribute access.
            or any((alias, symbol) in fact.used_attrs for alias in _aliases_for_module(fact, def_dotted))
            # Star import from the defining module that then uses the bare name.
            or (def_dotted in fact.star_from and symbol in fact.used_names)
        ):
            referencing.add(fact.dotted)
    return referencing


def resolved_reference_exists(
    symbol: str,
    def_dotted: str,
    facts: list[ModuleFacts],
    reexport_map: dict[str, set[str]],
) -> bool:
    """True when some runtime module holds a RESOLVED reference to ``symbol`` defined in ``def_dotted``."""
    return bool(referencing_modules(symbol, def_dotted, facts, reexport_map))


def resolve_identity(
    module: str, name: str, by_dotted: dict[str, ModuleFacts], visited: set[tuple[str, str]] | None = None
) -> tuple[str, str] | None:
    """Defining ``(module, name)`` of ``name`` exposed by ``module``, following re-exports; None when undefined.

    A name is defined where a public ``def``/``class`` has it; otherwise a from-import binding it (or a star import
    that exposes it) re-exports another module's name. ``visited`` makes import cycles terminate.
    """
    visited = set() if visited is None else visited
    if (module, name) in visited or module not in by_dotted:
        return None
    visited.add((module, name))
    fact = by_dotted[module]
    if name in fact.public_defs:
        return module, name
    for source, imported, asname in fact.from_imports:
        if asname == name and (source, imported) != (module, name):
            return resolve_identity(source, imported, by_dotted, visited)
    for star in sorted(fact.star_from):
        if found := resolve_identity(star, name, by_dotted, visited):
            return found
    return None


def build_reference_index(facts: list[ModuleFacts]) -> dict[tuple[str, str], set[str]]:
    """Map a defining ``(module, name)`` -> dotted modules that USE it (identity-aware, unlike :func:`referencing_modules`).

    A consumer counts only when the name it bound is loaded (``ast.Name`` Load, or ``alias.attr`` for a module
    alias): an unused import and an ``__all__`` string are not uses, and a re-export is followed to the defining
    module, so a homonym defined elsewhere is never credited. Every module (``__init__`` included) is a consumer iff
    it uses the name. A qualified chain longer than ``alias.attr`` (``pkg.mod.f()``) is not seen (errs to "unused").
    """
    by_dotted = {fact.dotted: fact for fact in facts if fact.dotted}
    index: dict[tuple[str, str], set[str]] = {}

    def add(module: str, name: str, consumer: str) -> None:
        identity = resolve_identity(module, name, by_dotted) or (module, name)
        index.setdefault(identity, set()).add(consumer)

    for fact in by_dotted.values():
        aliases: dict[str, str] = {}  # local name -> module it is bound to
        for module, name, asname in fact.from_imports:
            if f"{module}.{name}" in by_dotted and resolve_identity(module, name, by_dotted) is None:
                aliases[asname] = f"{module}.{name}"  # ``from pkg import submodule``
            elif asname in fact.used_names:
                add(module, name, fact.dotted)
        for module, asname in fact.plain_imports:
            if asname == module or asname != module.split(".")[0]:
                aliases[asname] = module  # ``import m`` / ``import a.b as x``
        for base, attr in fact.used_attrs:
            if base in aliases:
                add(aliases[base], attr, fact.dotted)
        for star in fact.star_from:
            for name in fact.used_names:
                add(star, name, fact.dotted)
    return index


def entry_point_targets() -> set[tuple[str, str]]:
    """``(module, symbol)`` pairs named by ``pyproject.toml`` entry points — consumed by the installer, not by src.

    Reads ``[project.scripts]``, ``[project.gui-scripts]`` and every ``[project.entry-points.<group>]``
    (``"pkg.mod:func"`` / ``"pkg.mod:Cls.method"`` -> ``("pkg.mod", "func"|"Cls")``).
    """
    pyproject = layout.REPO_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return set()
    project = tomllib.loads(pyproject.read_text(encoding="utf-8-sig")).get("project", {})
    groups = [project.get("scripts", {}), project.get("gui-scripts", {}), *project.get("entry-points", {}).values()]
    targets: set[tuple[str, str]] = set()
    for group in groups:
        for value in group.values():
            module, _sep, attr = str(value).partition(":")
            if attr:
                targets.add((module.strip(), attr.split("[")[0].strip().split(".")[0]))
    return targets


def vulture_whitelist_names() -> set[str]:
    """Names the repo's configured ``project.vulture_whitelist`` files declare as used.

    A Vulture whitelist is plain Python that merely *mentions* names (``foo``, ``_.attr``), so the
    names are its ``Name`` ids and ``Attribute`` attrs. Missing or unparseable files contribute nothing
    here — the Vulture gate is the one that fails on them.
    """
    names: set[str] = set()
    for relative in layout.project_paths("vulture_whitelist", []):
        path = layout.REPO_ROOT / relative
        if not path.is_file():
            continue
        tree = safe_parse(path.read_text(encoding="utf-8-sig"))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def runtime_corpus_files() -> list[Path]:
    """Every runtime ``src`` + ``scripts`` Python file, repo-relative (the resolver corpus)."""
    return layout.runtime_files()
