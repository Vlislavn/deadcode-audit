"""Whole-tree ADVISORY architecture rules: ``architecture/shallow-module`` and ``architecture/wide-directory``.

A *shallow module* has an interface about as wide as its implementation: most of what it contains is
reached from outside, so callers must learn nearly everything it does. Logic ported (not code) from
deslop-please (MIT) ``src/lint/shallow.rs``; reference resolution reuses the reachability resolver.

Judged units are Python packages (every runtime file under the directory; reported at ``__init__.py``)
and plain file modules (reported at the file). Within a unit:

* **working items** — top-level functions and the methods of top-level classes (classes themselves are
  never items; they only head lists). Excluded: dunders, ``@property``/setters, ``@overload``, ``@abstractmethod``,
  thin wrappers (:mod:`deadcode_audit.detectors.thin_wrapper`), and methods of classes with an external base.
* **shown** — public free functions USED by runtime code OUTSIDE the unit: the consumer must load a name that
  resolves, through re-exports, to this very definition (an unused import, an ``__all__`` entry, a same-named
  function elsewhere, or tests never count; a package ``__init__`` counts only when it uses the name). v1: methods count toward the total only, so the rule
  is false-negative-biased.
* **lists** — shown functions grouped by the in-unit class (defining module + name, own or from-imported) they mention: a function is on the list of EVERY such class (and counts once in ``shown``); one mentioning none forms a list of its own. A package whose items equal those of one
  of its file modules (facade + sole module) is skipped: the module is reported instead.

The rule fires when the interface is big (``shown`` over :data:`SHOWN_LIMIT`), it does not collapse into
few short lists, and little is hidden behind it (``hidden <= shown * BREAK_EVEN``).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from deadcode_audit import layout
from deadcode_audit.detectors import thin_wrapper
from deadcode_audit.diagnostic import (
    ENGINE_CODE_QUALITY,
    Diagnostic,
    RuleSpec,
    Severity,
    diagnostic_from_spec,
)
from deadcode_audit.framework import FileContext, safe_parse
from deadcode_audit.js import javascript
from deadcode_audit.structure.resolver import (
    ModuleFacts,
    build_reference_index,
    capped,
    collect_module_facts,
    resolve_identity,
    runtime_corpus_files,
)

SHOWN_LIMIT = 7  # fire only when MORE than this many items are shown
LISTS_LIMIT = 7  # ... and there are more lists than this ...
LIST_LENGTH_LIMIT = 7  # ... or some single list is longer than this
BREAK_EVEN = 1  # ... and hidden <= shown * BREAK_EVEN
NOTES_LIMIT = 7  # names listed in the finding's detail before "+N more"
MEMBERS_LIMIT = 15  # a directory with MORE direct members (source files + source-bearing subdirectories) is wide

SHALLOW_MODULE_RULE = RuleSpec(
    rule="architecture/shallow-module",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.WARNING,
    category="Architecture",
    help="Most of this module/package is reached from outside, so its interface is about as wide as its "
    "implementation. Hide helpers behind fewer entry points, or group related functions into a class.",
)

WIDE_DIRECTORY_RULE = RuleSpec(
    rule="architecture/wide-directory",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.WARNING,
    category="Architecture",
    help="This directory has too many direct members to hold in mind at once. Group related files into "
    "subpackages so each directory shows a handful of concepts.",
    style=True,
)

RULES = (SHALLOW_MODULE_RULE, WIDE_DIRECTORY_RULE)

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_EXCLUDED_DECORATORS = frozenset({"property", "setter", "getter", "deleter", "overload", "abstractmethod"})


@dataclass(frozen=True)
class _Item:
    name: str
    kind: str  # "function" | "method"
    names: frozenset[str] = frozenset()  # every Name id in a function (signature + body), for class grouping


@dataclass(frozen=True)
class _Module:
    dotted: str
    path: Path
    is_package: bool
    classes: frozenset[str]
    items: tuple[_Item, ...]


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _is_working(node: ast.FunctionDef | ast.AsyncFunctionDef, thin_lines: set[int]) -> bool:
    dunder = node.name.startswith("__") and node.name.endswith("__")
    return not (dunder or node.lineno in thin_lines or _EXCLUDED_DECORATORS & _decorator_names(node))


def _imported_from_repo(fact: ModuleFacts, name: str, known: set[str]) -> bool:
    """True when ``name`` is bound in ``fact`` by an import of a same-repo module (or a star import of one)."""
    for module, imported, asname in fact.from_imports:
        if asname == name:
            return module in known or f"{module}.{imported}" in known
    if any(asname == name and module in known for module, asname in fact.plain_imports):
        return True
    return any(star in known for star in fact.star_from)


def _has_external_base(node: ast.ClassDef, local: set[str], fact: ModuleFacts, known: set[str]) -> bool:
    for base in node.bases:
        root = base
        while isinstance(root, (ast.Attribute, ast.Subscript, ast.Call)):
            root = root.func if isinstance(root, ast.Call) else root.value
        if not isinstance(root, ast.Name):
            return True
        if root.id != "object" and root.id not in local and not _imported_from_repo(fact, root.id, known):
            return True
    return False


def _read_module(path: Path, source: str, tree: ast.Module, fact: ModuleFacts, known: set[str]) -> _Module:
    ctx = FileContext(path=path, source=source, tree=tree, lines=tuple(source.splitlines()))
    thin_lines = {d.line for d in thin_wrapper.detect(ctx)}
    local = {n.name for n in tree.body if isinstance(n, (*_FUNCS, ast.ClassDef))}
    items: list[_Item] = []
    for node in tree.body:
        if isinstance(node, _FUNCS) and _is_working(node, thin_lines):
            names = frozenset(n.id for n in ast.walk(node) if isinstance(n, ast.Name))
            items.append(_Item(node.name, "function", names))
        elif isinstance(node, ast.ClassDef):
            if not _has_external_base(node, local, fact, known):
                items.extend(
                    _Item(member.name, "method")
                    for member in node.body
                    if isinstance(member, _FUNCS) and _is_working(member, thin_lines)
                )
    classes = frozenset(n.name for n in tree.body if isinstance(n, ast.ClassDef))
    return _Module(fact.dotted, path, fact.is_package, classes, tuple(items))


def _units(modules: list[_Module]) -> dict[str, list[_Module]]:
    """Group modules into judged units: each package (all files under it) and each plain file module."""
    packages = {m.dotted for m in modules if m.is_package}
    units: dict[str, list[_Module]] = {}
    for module in modules:
        parts = module.dotted.split(".")
        for end in range(1, len(parts) + 1):
            prefix = ".".join(parts[:end])
            if prefix in packages or end == len(parts):
                units.setdefault(prefix, []).append(module)
    return units


def _item_set(members: list[_Module]) -> frozenset[tuple[str, str, str]]:
    return frozenset((m.dotted, item.name, item.kind) for m in members for item in m.items)


def _inside(dotted: str, unit: str) -> bool:
    return dotted == unit or dotted.startswith(f"{unit}.")


def _lists(
    shown: list[tuple[_Module, _Item]], members: list[_Module], by_dotted: dict[str, ModuleFacts]
) -> dict[tuple[str, str], list[str]]:
    """Group shown functions: each joins the list of EVERY in-unit class it mentions, else stands alone.

    A class is its defining ``(module, name)``: a name is the module's own class or a from-import that resolves to
    another in-unit module's class, so same-named classes in different files stay apart.
    """
    unit_classes = {(m.dotted, name) for m in members for name in m.classes}
    groups: dict[tuple[str, str], list[str]] = {}
    for module, item in shown:
        mentioned = {
            ident
            for name in item.names
            if (ident := (module.dotted, name) if name in module.classes else resolve_identity(module.dotted, name, by_dotted))
            in unit_classes
        }
        keys = [("class", ".".join(ident)) for ident in sorted(mentioned)] or [("function", f"{module.dotted}.{item.name}")]
        for key in keys:
            groups.setdefault(key, []).append(item.name)
    return groups


def shallow_module_diagnostics(files: list[Path]) -> list[Diagnostic]:
    """One advisory warning per shallow unit whose report location is among ``files`` (whole-tree judgement)."""
    sources = {path: layout.read_text(path) for path in runtime_corpus_files()}
    trees = {path: safe_parse(text) for path, text in sources.items()}
    by_path = {path: collect_module_facts(text, path, trees[path]) for path, text in sources.items()}
    facts = list(by_path.values())
    known = {fact.dotted for fact in facts}
    modules = [
        m
        for path, text in sources.items()
        if (tree := trees[path]) and (m := _read_module(path, text, tree, by_path[path], known)).dotted
    ]
    index = build_reference_index(facts)

    def referenced_outside(module: _Module, item: _Item, unit: str) -> bool:
        if item.kind == "method" or item.name.startswith("_"):
            return False
        return any(not _inside(dotted, unit) for dotted in index.get((module.dotted, item.name), ()))

    report_path = {m.dotted: m.path for m in modules}
    scanned = set(files)
    units = _units(modules)
    packages = {m.dotted for m in modules if m.is_package}
    module_items = {_item_set(members) for unit, members in units.items() if unit not in packages}
    diagnostics: list[Diagnostic] = []
    for unit, members in units.items():
        if report_path[unit] not in scanned or (unit in packages and _item_set(members) in module_items):
            continue  # not scanned, or a facade package that adds nothing over its sole module
        total = sum(len(m.items) for m in members)
        shown = [(m, item) for m in members for item in m.items if referenced_outside(m, item, unit)]
        hidden = total - len(shown)
        if len(shown) <= SHOWN_LIMIT or hidden > len(shown) * BREAK_EVEN:
            continue
        groups = _lists(shown, members, {f.dotted: f for f in facts})
        longest = max(groups.values(), key=len)
        if len(groups) <= LISTS_LIMIT and len(longest) <= LIST_LENGTH_LIMIT:
            continue
        kind = "package" if unit in packages else "module"
        diagnostics.append(
            diagnostic_from_spec(
                SHALLOW_MODULE_RULE,
                file_path=report_path[unit].as_posix(),
                line=1,
                message=(
                    f"{kind} '{unit}' exposes {len(shown)} of {total} working items to outside code "
                    f"({hidden} hidden) in {len(groups)} list(s), longest {len(longest)}"
                ),
                detail=f"shown: {capped([item.name for _m, item in shown], NOTES_LIMIT)}",
            )
        )
    return sorted(diagnostics, key=lambda d: (d.file_path, d.line))


def _is_test_source(path: Path) -> bool:
    """Test files of any language: ``*_test.py``, ``*.test.*``, ``*.spec.*`` and :func:`javascript.is_js_test_file`."""
    return path.stem.endswith("_test") or ".test." in path.name or ".spec." in path.name or javascript.is_js_test_file(path)


def wide_directory_diagnostics(files: list[Path]) -> list[Diagnostic]:
    """One advisory warning per directory with more than :data:`MEMBERS_LIMIT` direct members.

    Members are source files (not ``__init__.py``) plus subdirectories holding some, counted from disk (the shared
    runtime walker: source roots, no test/vendored/cache paths), so a
    ``--changed`` scan sees the whole directory; a directory is reported when a scanned non-test source file (Python,
    JavaScript or TypeScript) lies under it, at its
    ``__init__.py`` if present, else at the directory itself.
    """
    roots = set(layout.source_roots())
    file_members: dict[Path, list[str]] = defaultdict(list)
    dir_members: dict[Path, set[str]] = defaultdict(set)
    for path in layout.runtime_files(suffixes=javascript.SUFFIXES | {".py"}):
        if path.name == "__init__.py" or _is_test_source(path):
            continue  # a package marker is not a member; test files are not members
        file_members[path.parent].append(path.name)
        child = path.parent
        while child not in roots and child != Path("."):
            dir_members[child.parent].add(child.name)
            child = child.parent
    triggers = [f for f in files if not _is_test_source(f)]
    diagnostics: list[Diagnostic] = []
    for directory in sorted(file_members.keys() | dir_members.keys()):
        names, subdirs = sorted(file_members[directory]), sorted(dir_members[directory])
        if len(names) + len(subdirs) <= MEMBERS_LIMIT or not any(f.is_relative_to(directory) for f in triggers):
            continue
        init = directory / "__init__.py"
        diagnostics.append(
            diagnostic_from_spec(
                WIDE_DIRECTORY_RULE,
                file_path=(init if (layout.REPO_ROOT / init).is_file() else directory).as_posix(),
                line=1,
                message=f"directory '{directory.as_posix()}' has {len(names) + len(subdirs)} direct members "
                f"({len(names)} files, {len(subdirs)} subdirectories; limit {MEMBERS_LIMIT})",
                detail=f"files: {capped(names, NOTES_LIMIT)}"
                + (f"; dirs: {capped(subdirs, NOTES_LIMIT)}" if subdirs else ""),
            )
        )
    return diagnostics

