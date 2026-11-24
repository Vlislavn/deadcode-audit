"""Whole-tree ADVISORY architecture rules (``architecture/shallow-module``).

A *shallow module* has an interface about as wide as its implementation: most of what it contains is
reached from outside, so callers must learn nearly everything it does. Logic ported (not code) from
deslop-please (MIT) ``src/lint/shallow.rs``; reference resolution reuses the reachability resolver.

Judged units are Python packages (every runtime file under the directory; reported at ``__init__.py``)
and plain file modules (reported at the file). Within a unit:

* **working items** — top-level functions, public classes (one constructor unit each) and the methods
  of top-level classes. Excluded: dunders, ``@property``/setters, ``@overload``, ``@abstractmethod``,
  thin wrappers (:mod:`deadcode_audit.detectors.thin_wrapper`), and methods of classes with an external base.
* **shown** — public free functions / public classes referenced by runtime code OUTSIDE the unit (tests
  and package ``__init__`` facades never count). v1: methods count toward the total only, so the rule
  is false-negative-biased.
* **lists** — shown functions grouped by the in-unit class they mention (a shown class heads its own
  list); a function mentioning none forms a list of its own.

The rule fires when the interface is big (``shown`` over :data:`SHOWN_LIMIT`), it does not collapse into
few short lists, and little is hidden behind it (``hidden <= shown * BREAK_EVEN``).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from deadcode_audit import diffscope
from deadcode_audit.detectors import thin_wrapper
from deadcode_audit.diagnostic import ENGINE_CODE_QUALITY, Diagnostic, RuleSpec, Severity, diagnostic_from_spec
from deadcode_audit.framework import FileContext, safe_parse
from deadcode_audit.reachability import (
    ModuleFacts,
    build_reexport_map,
    collect_module_facts,
    referencing_modules,
    runtime_corpus_files,
)

SHOWN_LIMIT = 7  # fire only when MORE than this many items are shown
LISTS_LIMIT = 7  # ... and there are more lists than this ...
LIST_LENGTH_LIMIT = 7  # ... or some single list is longer than this
BREAK_EVEN = 1  # ... and hidden <= shown * BREAK_EVEN
NOTES_LIMIT = 7  # names listed in the finding's detail before "+N more"

SHALLOW_MODULE_RULE = RuleSpec(
    rule="architecture/shallow-module",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.WARNING,
    category="Architecture",
    help="Most of this module/package is reached from outside, so its interface is about as wide as its "
    "implementation. Hide helpers behind fewer entry points, or group related functions into a class.",
)

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_EXCLUDED_DECORATORS = frozenset({"property", "setter", "getter", "deleter", "overload", "abstractmethod"})


@dataclass(frozen=True)
class _Item:
    name: str
    kind: str  # "function" | "class" | "method"
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


def _read_module(path: Path, source: str, known: set[str]) -> _Module | None:
    tree = safe_parse(source)
    if tree is None:
        return None
    fact = collect_module_facts(source, path)
    ctx = FileContext(path=path, source=source, tree=tree, lines=tuple(source.splitlines()))
    thin_lines = {d.line for d in thin_wrapper.detect(ctx)}
    local = {n.name for n in tree.body if isinstance(n, (*_FUNCS, ast.ClassDef))}
    items: list[_Item] = []
    for node in tree.body:
        if isinstance(node, _FUNCS) and _is_working(node, thin_lines):
            names = frozenset(n.id for n in ast.walk(node) if isinstance(n, ast.Name))
            items.append(_Item(node.name, "function", names))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                items.append(_Item(node.name, "class"))
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


def _inside(dotted: str, unit: str) -> bool:
    return dotted == unit or dotted.startswith(f"{unit}.")


def _cap(names: list[str]) -> str:
    more = len(names) - NOTES_LIMIT
    return ", ".join(names[:NOTES_LIMIT]) + (f", +{more} more" if more > 0 else "")


def _lists(shown: list[tuple[_Module, _Item]], classes: set[str]) -> dict[tuple[str, str], list[str]]:
    """Group shown items: a class heads its own list; a function joins the in-unit class it mentions, else stands alone."""
    groups: dict[tuple[str, str], list[str]] = {}
    for _module, item in shown:
        if item.kind == "class":
            groups[("class", item.name)] = [item.name]
    for module, item in shown:
        if item.kind == "function":
            mentioned = item.names & classes
            key = ("class", min(mentioned)) if mentioned else ("function", f"{module.dotted}.{item.name}")
            groups.setdefault(key, []).append(item.name)
    return groups


def shallow_module_diagnostics(files: list[Path]) -> list[Diagnostic]:
    """One advisory warning per shallow unit whose report location is among ``files`` (whole-tree judgement)."""
    sources = {path: diffscope.read_text(path) for path in runtime_corpus_files()}
    facts = [collect_module_facts(text, path) for path, text in sources.items()]
    known = {fact.dotted for fact in facts}
    modules = [m for path, text in sources.items() if (m := _read_module(path, text, known)) and m.dotted]
    reexport_map = build_reexport_map(facts)
    facades = {fact.dotted for fact in facts if fact.is_package}  # an __init__ re-export is not a consumer
    cache: dict[tuple[str, str], set[str]] = {}

    def referenced_outside(module: _Module, item: _Item, unit: str) -> bool:
        if item.kind == "method" or item.name.startswith("_"):
            return False
        key = (module.dotted, item.name)
        if key not in cache:
            cache[key] = referencing_modules(item.name, module.dotted, facts, reexport_map) - facades
        return any(not _inside(dotted, unit) for dotted in cache[key])

    report_path = {m.dotted: m.path for m in modules}
    scanned = set(files)
    diagnostics: list[Diagnostic] = []
    for unit, members in _units(modules).items():
        if report_path[unit] not in scanned:
            continue
        total = sum(len(m.items) for m in members)
        shown = [(m, item) for m in members for item in m.items if referenced_outside(m, item, unit)]
        hidden = total - len(shown)
        if len(shown) <= SHOWN_LIMIT or hidden > len(shown) * BREAK_EVEN:
            continue
        groups = _lists(shown, set().union(*(m.classes for m in members)))
        longest = max(groups.values(), key=len)
        if len(groups) <= LISTS_LIMIT and len(longest) <= LIST_LENGTH_LIMIT:
            continue
        kind = "package" if any(m.is_package and m.dotted == unit for m in members) else "module"
        diagnostics.append(
            diagnostic_from_spec(
                SHALLOW_MODULE_RULE,
                file_path=report_path[unit].as_posix(),
                line=1,
                message=(
                    f"{kind} '{unit}' exposes {len(shown)} of {total} working items to outside code "
                    f"({hidden} hidden) in {len(groups)} list(s), longest {len(longest)}"
                ),
                detail=f"shown: {_cap([item.name for _m, item in shown])}",
            )
        )
    return sorted(diagnostics, key=lambda d: (d.file_path, d.line))
