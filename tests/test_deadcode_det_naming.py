"""Tests for the ``ai-slop/generic-naming`` detector (scripts/deadcode/detectors/naming.py).

Each rule gets >=3 positive cases (varied identifiers, proving the principle generalises) and >=3
adversarial negatives (legitimate near-misses that must stay quiet), plus an explicit generalisation
negative that a naive substring match would wrongly flag.
"""

from __future__ import annotations

from pathlib import Path

from deadcode_audit.detectors.naming import SPEC, detect
from deadcode_audit.framework import build_file_context

RULE = SPEC.rule


def _fire_lines(source: str) -> list[int]:
    """Return the sorted line numbers on which ``generic-naming`` fires for ``source``."""
    ctx = build_file_context(Path("src/x.py"), source)
    return sorted(d.line for d in detect(ctx) if d.rule == RULE)


def _fires(source: str) -> bool:
    """True iff the detector emits at least one ``generic-naming`` finding."""
    return bool(_fire_lines(source))


# --------------------------------------------------------------------------------------------------
# Pattern (a): generic stem + numeric suffix — POSITIVE
# --------------------------------------------------------------------------------------------------


def test_pos_function_with_stem_counter_underscore() -> None:
    assert _fires("def helper_2():\n    return 1\n")


def test_pos_class_with_stem_counter_no_underscore() -> None:
    assert _fires("class Data1:\n    pass\n")  # case-insensitive: 'Data1' matches stem 'data'


def test_pos_module_assignment_stem_counter() -> None:
    assert _fires("result_3 = compute()\n")


def test_pos_varied_stems_all_fire() -> None:
    # Different stems + different suffix shapes, proving it is not one hardcoded snippet.
    for source in ("def tmp1():\n    return 0\n", "value_42 = read()\n", "class Item07:\n    pass\n"):
        assert _fires(source), source


def test_pos_async_def_stem_counter() -> None:
    assert _fires("async def func2():\n    return None\n")


def test_pos_tuple_unpack_module_assignment() -> None:
    # Module-level tuple unpacking flattens to each bound name.
    assert _fire_lines("temp1, keep = split()\n") == [1]


# --------------------------------------------------------------------------------------------------
# Pattern (a) — ADVERSARIAL NEGATIVE (legitimate near-misses)
# --------------------------------------------------------------------------------------------------


def test_neg_bare_generic_noun_without_suffix() -> None:
    # 'data' / 'result' / 'value' ALONE are domain-fine — only the counter shape is empty.
    assert not _fires("data = load()\nresult = run()\nvalue = pick()\n")


def test_neg_local_variable_is_out_of_scope() -> None:
    # 'tmp1' as a LOCAL inside a function is not flagged — rule is module-level/defs only.
    assert not _fires("def process(rows):\n    tmp1 = rows[0]\n    return tmp1\n")


def test_neg_for_target_and_comprehension_not_flagged() -> None:
    src = "TOTAL = 0\nfor item1 in things:\n    TOTAL += item1\nsquares = [val1 for val1 in nums]\n"
    assert not _fires(src)


def test_neg_attribute_and_subscript_targets() -> None:
    # Assigning to self.data1 / d['result2'] introduces no new module name.
    src = "class Box:\n    def set(self):\n        self.data1 = 1\nregistry = {}\nregistry['result2'] = 9\n"
    assert not _fires(src)


def test_neg_dunder_names() -> None:
    assert not _fires("__all__ = ['x']\n\ndef __init__():\n    pass\n")


def test_neg_version_like_constant_is_meaningful() -> None:
    # A real domain word that merely ends in a digit (not a generic stem) must not fire.
    assert not _fires("HTTP2 = 'h2'\nSHA256 = 'sha256'\nutf8 = 'utf-8'\n")


# --------------------------------------------------------------------------------------------------
# Pattern (b): metasyntactic placeholder as a def/class name — POSITIVE
# --------------------------------------------------------------------------------------------------


def test_pos_def_named_foo() -> None:
    assert _fires("def foo():\n    return 1\n")


def test_pos_class_named_bar() -> None:
    assert _fires("class Bar:\n    pass\n")


def test_pos_varied_placeholders_all_fire() -> None:
    for source in ("def baz():\n    pass\n", "def qux():\n    pass\n", "class Foobar:\n    pass\n"):
        assert _fires(source), source


def test_pos_test_fixture_named_foo_is_flagged() -> None:
    # Test fixtures are fine, but a def literally named 'foo' is still flagged (per spec).
    src = "import pytest\n\n@pytest.fixture\ndef foo():\n    return 1\n"
    assert _fires(src)


# --------------------------------------------------------------------------------------------------
# Pattern (b) — ADVERSARIAL NEGATIVE
# --------------------------------------------------------------------------------------------------


def test_neg_placeholder_as_module_assignment_not_flagged() -> None:
    # (b) is scoped to def/class names only; a module constant 'foo' stays conservative/quiet.
    assert not _fires("foo = 1\nbar = 2\n")


def test_neg_real_words_containing_placeholder_substring() -> None:
    # 'foothold', 'barometer', 'bazaar' contain a placeholder substring but are real words.
    src = "def foothold():\n    pass\n\nclass Barometer:\n    pass\n\ndef bazaar():\n    pass\n"
    assert not _fires(src)


def test_neg_placeholder_with_meaningful_suffix() -> None:
    # 'foo_loader' / 'bar_client' are compound names, not the bare placeholder.
    src = "def foo_loader():\n    pass\n\nclass BarClient:\n    pass\n"
    assert not _fires(src)


def test_neg_single_letter_loop_index() -> None:
    # i/j/k can never match either pattern; the rule must stay silent.
    assert not _fires(
        "def walk(grid):\n    for i in range(3):\n        for j in range(3):\n            grid[i][j] = 0\n"
    )


# --------------------------------------------------------------------------------------------------
# Explicit generalisation negative: a naive snippet match would trip, the principled rule must not.
# --------------------------------------------------------------------------------------------------


def test_generalisation_substring_stems_not_flagged() -> None:
    # 'metadata', 'datapoint', 'result_set', 'foobar_handler', 'temperature', 'itemize' all contain a
    # generic stem as a SUBSTRING but are meaningful whole identifiers — a `^stem$`-anchored, counter-
    # requiring rule must leave every one of them alone.
    src = (
        "metadata = {}\n"
        "datapoint = 0\n"
        "result_set = []\n"
        "def temperature():\n    return 0\n"
        "def itemize(rows):\n    return rows\n"
        "class FoobarHandler:\n    pass\n"
    )
    assert not _fires(src)


def test_finding_carries_spec_metadata() -> None:
    # Sanity: emitted diagnostics inherit the spec's engine/severity/help and a 1-based line.
    ctx = build_file_context(Path("src/x.py"), "def helper_9():\n    return 1\n")
    findings = [d for d in detect(ctx) if d.rule == RULE]
    assert len(findings) == 1
    finding = findings[0]
    assert finding.engine == SPEC.engine
    assert finding.severity == SPEC.default_severity
    assert finding.help == SPEC.help
    assert finding.line == 1
    assert finding.file_path == "src/x.py"
