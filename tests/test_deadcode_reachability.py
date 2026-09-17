"""Tests for the Tier 4 import-resolver (advisory refinement of the runtime-consumer gate).

The resolver decides consumed-vs-advisory; it is NEVER the blocking authority (the grep floor
is — see test_dead_code_guard.py::symbols_without_runtime_consumers tests). These tests pin the
resolver across the reference patterns the non-overfitting gate flagged as must-handle:
re-exports, qualified attribute access, aliased imports, intra-module use, star imports — and
the same-name COLLISION case that must NOT resolve. Symbol/module names are deliberately varied
(not the motivating trace's names) to prove the rules generalise.
"""

from pathlib import Path

import pytest

from deadcode_audit.reachability import (
    build_reexport_map,
    collect_module_facts,
    module_dotted_name,
    resolved_reference_exists,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (Path("src/modules/core/config.py"), "modules.core.config"),
        (Path("src/modules/core/__init__.py"), "modules.core"),
        (Path("src/deadcode_audit/diffscope.py"), "deadcode_audit.diffscope"),
        (Path("src/deadcode_audit/__init__.py"), "deadcode_audit"),
        (Path("tests/scripts/test_x.py"), None),  # non-runtime
        (Path("src/modules/data.json"), None),  # non-python
    ],
)
def test_module_dotted_name(path: Path, expected: str | None) -> None:
    assert module_dotted_name(path) == expected


def test_collect_module_facts_extracts_imports_uses_and_all() -> None:
    facts = collect_module_facts(
        "from modules.alpha.beta import widget as w\n"
        "from modules.alpha import beta\n"
        "import modules.gamma as g\n"
        "from modules.delta import *\n"
        "__all__ = ['exported']\n"
        "def exported():\n    w()\n    beta.thing()\n    g.helper()\n",
        Path("src/modules/consumer.py"),
    )
    assert ("modules.alpha.beta", "widget", "w") in facts.from_imports
    assert ("modules.alpha", "beta", "beta") in facts.from_imports
    assert ("modules.gamma", "g") in facts.plain_imports
    assert "modules.delta" in facts.star_from
    assert facts.dunder_all == frozenset({"exported"})
    assert "exported" in facts.public_defs
    assert "w" in facts.used_names
    assert ("beta", "thing") in facts.used_attrs
    assert ("g", "helper") in facts.used_attrs


def test_relative_import_resolves_to_absolute_in_package_init() -> None:
    facts = collect_module_facts(
        "from .config import Settings\n",
        Path("src/modules/core/__init__.py"),
    )
    assert ("modules.core.config", "Settings", "Settings") in facts.from_imports


def test_relative_import_resolves_from_submodule() -> None:
    facts = collect_module_facts(
        "from .sibling import helper\n",
        Path("src/modules/core/service.py"),
    )
    assert ("modules.core.sibling", "helper", "helper") in facts.from_imports


def _facts(*pairs: tuple[str, str]) -> list:
    return [collect_module_facts(src, Path(path)) for src, path in pairs]


def test_direct_from_import_resolves() -> None:
    facts = _facts(
        ("def widget():\n    return 1\n", "src/modules/alpha.py"),
        ("from modules.alpha import widget\nwidget()\n", "src/modules/consumer.py"),
    )
    assert resolved_reference_exists("widget", "modules.alpha", facts, build_reexport_map(facts))


def test_aliased_from_import_resolves() -> None:
    facts = _facts(
        ("def widget():\n    return 1\n", "src/modules/alpha.py"),
        ("from modules.alpha import widget as w\nw()\n", "src/modules/consumer.py"),
    )
    assert resolved_reference_exists("widget", "modules.alpha", facts, build_reexport_map(facts))


def test_reexport_through_package_init_resolves() -> None:
    """A symbol consumed via ``from package import S`` (re-exported by the package __init__)."""
    facts = _facts(
        ("def gadget():\n    return 1\n", "src/modules/sub/impl.py"),
        (
            "from modules.sub.impl import gadget\n__all__ = ['gadget']\n",
            "src/modules/sub/__init__.py",
        ),
        ("from modules.sub import gadget\ngadget()\n", "src/modules/top.py"),
    )
    assert resolved_reference_exists("gadget", "modules.sub.impl", facts, build_reexport_map(facts))


def test_qualified_attribute_via_from_package_import_submodule_resolves() -> None:
    """The repo's own convention: ``from pkg import submodule; submodule.symbol(...)``."""
    facts = _facts(
        ("def changed_files(b):\n    return []\n", "scripts/deadcode/diffscope.py"),
        (
            "from deadcode_audit import diffscope\ndiffscope.changed_files('main')\n",
            "scripts/deadcode/cli.py",
        ),
    )
    assert resolved_reference_exists("changed_files", "deadcode_audit.diffscope", facts, build_reexport_map(facts))


def test_qualified_attribute_via_plain_import_resolves() -> None:
    facts = _facts(
        ("def builder():\n    return 1\n", "src/modules/factory.py"),
        ("import modules.factory as f\nf.builder()\n", "src/modules/consumer.py"),
    )
    assert resolved_reference_exists("builder", "modules.factory", facts, build_reexport_map(facts))


def test_star_import_then_use_resolves() -> None:
    facts = _facts(
        ("def sprocket():\n    return 1\n", "src/modules/parts.py"),
        ("from modules.parts import *\nsprocket()\n", "src/modules/consumer.py"),
    )
    assert resolved_reference_exists("sprocket", "modules.parts", facts, build_reexport_map(facts))


def test_intra_module_use_resolves() -> None:
    facts = _facts(
        (
            "def lever():\n    return 1\n\n\ndef pull():\n    return lever()\n",
            "src/modules/machine.py",
        ),
    )
    assert resolved_reference_exists("lever", "modules.machine", facts, build_reexport_map(facts))


def test_same_name_collision_does_not_resolve() -> None:
    """A dead ``run`` in one module is NOT resolved by a same-named method elsewhere (the FN class)."""
    facts = _facts(
        ("def run():\n    return 1\n", "src/modules/dead.py"),
        (
            "class Worker:\n    def run(self):\n        return 2\n",
            "src/modules/worker.py",
        ),
    )
    # 'run' is a name elsewhere, but nothing references modules.dead.run -> unresolved -> advisory.
    assert not resolved_reference_exists("run", "modules.dead", facts, build_reexport_map(facts))


def test_dynamic_getattr_string_does_not_resolve() -> None:
    """A getattr/string-registry consumer is not statically resolvable (-> advisory, never blocked)."""
    facts = _facts(
        ("def handler():\n    return 1\n", "src/modules/handlers.py"),
        (
            "import modules.handlers as h\nname = 'handler'\nfn = getattr(h, name)\n",
            "src/modules/dispatch.py",
        ),
    )
    assert not resolved_reference_exists("handler", "modules.handlers", facts, build_reexport_map(facts))


def test_no_reference_anywhere_does_not_resolve() -> None:
    facts = _facts(
        ("def orphan():\n    return 1\n", "src/modules/lonely.py"),
        ("def something_else():\n    return 2\n", "src/modules/other.py"),
    )
    assert not resolved_reference_exists("orphan", "modules.lonely", facts, build_reexport_map(facts))


# --- classify_unresolved: the pure classifier shared by the gate and the whole-tree scan ---


def test_classify_unresolved_orphaned_policy_and_decorated_precedence() -> None:
    """The fork that distinguishes the blocking gate (orphaned->block) from the scan (orphaned->advisory)."""
    from deadcode_audit import reachability as r

    plain = r.PublicSymbolDefinition(Path("src/x.py"), "lonely", 1, decorated=False)
    decorated = r.PublicSymbolDefinition(Path("src/x.py"), "handler", 1, decorated=True)

    # diff-scoped gate: a truly-orphaned undecorated symbol hard-blocks
    assert r.classify_unresolved(plain, occurs_in_runtime=False, has_test_ref=False, orphaned_blocks=True) == (
        "block",
        None,
    )
    # whole-tree scan: the SAME symbol becomes the top advisory, never blocks
    assert r.classify_unresolved(plain, occurs_in_runtime=False, has_test_ref=False, orphaned_blocks=False) == (
        "advisory",
        r._ADVISORY_ORPHANED,
    )
    # decoration takes precedence over orphaned — a decorated orphan is framework-live, never blocked
    assert r.classify_unresolved(decorated, occurs_in_runtime=False, has_test_ref=False, orphaned_blocks=True) == (
        "advisory",
        r._ADVISORY_DECORATED,
    )
    # occurs in runtime (collision/dynamic) + undecorated -> unresolved suspect
    assert r.classify_unresolved(plain, occurs_in_runtime=True, has_test_ref=False, orphaned_blocks=True) == (
        "advisory",
        r._ADVISORY_UNRESOLVED,
    )
    # test-only + the *_for_tests convention -> test-support, regardless of orphaned policy
    hook = r.PublicSymbolDefinition(Path("src/x.py"), "reset_for_tests", 1, decorated=False)
    assert r.classify_unresolved(hook, occurs_in_runtime=False, has_test_ref=True, orphaned_blocks=False) == (
        "advisory",
        r._ADVISORY_TEST_SUPPORT,
    )


# --- runtime_consumer_check: BLOCK only truly-orphaned; test-only-consumed is advisory ---


def _drive_consumer_check(monkeypatch: pytest.MonkeyPatch, *, has_test_ref: bool) -> tuple[int, str]:
    """Drive runtime_consumer_check with a single unresolved, no-runtime-consumer public symbol."""
    from deadcode_audit import reachability as r

    monkeypatch.setattr(
        r,
        "public_symbol_definitions",
        lambda _p: [r.PublicSymbolDefinition(Path("src/x.py"), "thing", 10, False)],
    )
    monkeypatch.setattr(r, "_runtime_reference_locations", lambda _s: [])  # no src/scripts consumer
    monkeypatch.setattr(r, "runtime_corpus_files", lambda: [])
    monkeypatch.setattr(r, "resolved_reference_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(r, "_has_test_reference", lambda _s: has_test_ref)
    return r.runtime_consumer_check([Path("src/x.py")])


def test_consumer_check_truly_orphaned_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _drive_consumer_check(monkeypatch, has_test_ref=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "Runtime consumer check failed" in out


def test_consumer_check_test_only_is_advisory_not_blocking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A symbol referenced only by tests (a test-support helper or unwired API) must NOT hard-block.
    rc = _drive_consumer_check(monkeypatch, has_test_ref=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "referenced only by tests" in out


def test_consumer_check_ranks_advisories_by_deadness_confidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advisories print most- to least-likely-dead: test-only-reachable > unresolved > decorated.

    Symbol names here (``staged_export`` / ``ambient_label`` / ``mounted_route``) are deliberately
    unrelated to any real trace — the ranking keys off the advisory CLASS, not any value, so the
    order must hold for any mix of the three classes.
    """
    from deadcode_audit import reachability as r

    defs = {
        Path("src/route.py"): r.PublicSymbolDefinition(Path("src/route.py"), "mounted_route", 5, decorated=True),
        Path("src/api.py"): r.PublicSymbolDefinition(Path("src/api.py"), "staged_export", 7, decorated=False),
        Path("src/dup.py"): r.PublicSymbolDefinition(Path("src/dup.py"), "ambient_label", 9, decorated=False),
    }
    monkeypatch.setattr(r, "public_symbol_definitions", lambda p: [defs[p]])
    # Only ``ambient_label`` occurs elsewhere in runtime code (so it is "unresolved", not orphaned);
    # the other two have no runtime occurrence at all.
    monkeypatch.setattr(
        r,
        "_runtime_reference_locations",
        lambda s: [(Path("src/elsewhere.py"), 1)] if s == "ambient_label" else [],
    )
    monkeypatch.setattr(r, "runtime_corpus_files", lambda: [])
    monkeypatch.setattr(r, "resolved_reference_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(r, "_has_test_reference", lambda s: s == "staged_export")  # only the export is test-covered

    rc = r.runtime_consumer_check(list(defs))
    out = capsys.readouterr().out

    assert rc == 0  # every symbol is advisory; nothing hard-blocks
    # per-line order: test-only (staged_export) < unresolved (ambient_label) < decorated (mounted_route)
    assert out.index("staged_export") < out.index("ambient_label") < out.index("mounted_route")
    # a per-class confidence summary is printed, highest-confidence label first
    assert out.index("test-only-reachable") < out.index("name unresolved") < out.index("decorated  ")
    assert "[  1] test-only-reachable" in out  # exactly one symbol in the actionable class


def test_consumer_check_demotes_test_support_named_symbols(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``*_for_testing`` symbol consumed only by tests is an intentional hook, not product-dead.

    The name is ``refresh_for_testing`` — deliberately NOT the motivating ``reset_for_tests`` — so the
    classification keys off the convention SUFFIX, not a specific symbol. It must land in the
    test-support class (expected), never in the product-dead "remove or wire" group.
    """
    from deadcode_audit import reachability as r

    monkeypatch.setattr(
        r,
        "public_symbol_definitions",
        lambda _p: [r.PublicSymbolDefinition(Path("src/x.py"), "refresh_for_testing", 12, False)],
    )
    monkeypatch.setattr(r, "_runtime_reference_locations", lambda _s: [])  # no src/scripts consumer
    monkeypatch.setattr(r, "runtime_corpus_files", lambda: [])
    monkeypatch.setattr(r, "resolved_reference_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(r, "_has_test_reference", lambda _s: True)

    rc = r.runtime_consumer_check([Path("src/x.py")])
    out = capsys.readouterr().out

    assert rc == 0
    assert "test-support hook" in out  # summary label printed
    assert "refresh_for_testing -> no runtime consumer; named *_for_tests" in out
    # and it is NOT mislabelled as a product-dead candidate
    assert "referenced only by tests" not in out
