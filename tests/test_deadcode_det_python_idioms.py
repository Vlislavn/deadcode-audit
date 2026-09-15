"""Tests for the ``python_idioms`` AI-slop detector.

Each rule gets >=3 positive cases (varied identifiers/types — proving the rule encodes a
structural principle, not one snippet) and >=3 adversarial negatives (legitimate near-misses
that must NOT fire), plus an explicit "generalisation" negative that a naive snippet match would
trip on. All assertions go through ``detect`` against a real ``build_file_context``.
"""

from __future__ import annotations

from pathlib import Path

from deadcode_audit.detectors.python_idioms import (
    CHAINED_GET_SPEC,
    ISINSTANCE_LADDER_SPEC,
    RANGE_LEN_SPEC,
    REPETITIVE_DISPATCH_SPEC,
    detect,
)
from deadcode_audit.framework import build_file_context


def _rule_lines(source: str, rule: str) -> list[int]:
    """Return the (sorted) 1-based lines at which ``rule`` fires for ``source``."""
    ctx = build_file_context(Path("src/x.py"), source)
    return sorted(d.line for d in detect(ctx) if d.rule == rule)


def _fires(source: str, rule: str) -> bool:
    return bool(_rule_lines(source, rule))


# --------------------------------------------------------------------------------------
# ai-slop/python-range-len-loop
# --------------------------------------------------------------------------------------

RANGE_LEN = RANGE_LEN_SPEC.rule


def test_range_len_fires_basic_index_loop() -> None:
    src = "def f(items):\n    for i in range(len(items)):\n        print(items[i])\n"
    assert _fires(src, RANGE_LEN)


def test_range_len_fires_other_identifiers() -> None:
    src = "def g(rows):\n    for idx in range(len(rows)):\n        handle(rows[idx])\n"
    assert _fires(src, RANGE_LEN)


def test_range_len_fires_async_for() -> None:
    src = "async def h(buffer):\n" "    async for k in range(len(buffer)):\n" "        await use(buffer[k])\n"
    assert _fires(src, RANGE_LEN)


def test_range_len_not_fires_with_start_arg() -> None:
    # range(1, len(x)) carries an intentional start -> not the manual-index idiom.
    src = "def f(items):\n    for i in range(1, len(items)):\n        print(items[i])\n"
    assert not _fires(src, RANGE_LEN)


def test_range_len_not_fires_with_step_arg() -> None:
    src = "def f(items):\n    for i in range(0, len(items), 2):\n        print(items[i])\n"
    assert not _fires(src, RANGE_LEN)


def test_range_len_not_fires_plain_range_count() -> None:
    src = "def f(n):\n    for i in range(n):\n        print(i)\n"
    assert not _fires(src, RANGE_LEN)


def test_range_len_generalisation_attribute_range_not_fired() -> None:
    # A naive 'range(len(' substring match would trip on this, but obj.range / np.len are
    # different APIs (not the builtins) and must NOT fire.
    src = "def f(obj, data):\n    for i in obj.range(np.len(data)):\n        use(i)\n"
    assert not _fires(src, RANGE_LEN)


# --------------------------------------------------------------------------------------
# ai-slop/python-chained-dict-get
# --------------------------------------------------------------------------------------

CHAINED_GET = CHAINED_GET_SPEC.rule


def test_chained_get_spares_empty_dict_default() -> None:
    # Tuned: an explicit `{}` intermediate default is crash-safe defensive navigation, not slop.
    src = "def f(cfg):\n    return cfg.get('a', {}).get('b')\n"
    assert not _fires(src, CHAINED_GET)


def test_chained_get_fires_no_intermediate_default() -> None:
    src = "def f(payload):\n    return payload.get('user').get('name')\n"
    assert _fires(src, CHAINED_GET)


def test_chained_get_spares_any_explicit_intermediate_default() -> None:
    # Tuned: any explicit default (here `{}` plus an outer default) is a deliberate safe chain.
    src = "def f(doc):\n    return doc.get('meta', {}).get('owner', 'anon')\n"
    assert not _fires(src, CHAINED_GET)


def test_chained_get_not_fires_single_get() -> None:
    src = "def f(d):\n    return d.get('a', {})\n"
    assert not _fires(src, CHAINED_GET)


def test_chained_get_not_fires_real_default_not_rechained() -> None:
    # A real non-{} default that is NOT then re-.get()'d is a deliberate fallback -> fine.
    src = "def f(d):\n    value = d.get('a', [])\n    return value\n"
    assert not _fires(src, CHAINED_GET)


def test_chained_get_not_fires_when_intermediate_has_real_default() -> None:
    # Inner get supplies a real, chainable default object; the chain is intentional.
    src = "def f(d, fallback):\n    return d.get('a', fallback).get('b')\n"
    assert not _fires(src, CHAINED_GET)


def test_chained_get_generalisation_unrelated_method_chain_not_fired() -> None:
    # Two .get calls on DIFFERENT receivers (not a receiver-is-get chain) must not fire,
    # even though 'get(...).get(' appears textually after the first call returns.
    src = "def f(a, b):\n    x = a.get('k')\n    y = b.get('k')\n    return x, y\n"
    assert not _fires(src, CHAINED_GET)


def test_chained_get_generalisation_get_with_kwargs_not_dict_get() -> None:
    # requests.get(url, params=...) is not dict.get; chaining .json().get must not be flagged.
    src = "def f(session, url):\n    return session.get(url, timeout=5).json()\n"
    assert not _fires(src, CHAINED_GET)


# --------------------------------------------------------------------------------------
# ai-slop/python-repetitive-dispatch
# --------------------------------------------------------------------------------------

REPETITIVE = REPETITIVE_DISPATCH_SPEC.rule


def test_repetitive_dispatch_fires_eq_chain() -> None:
    src = (
        "def route(cmd):\n"
        "    if cmd == 'add':\n        return do_add()\n"
        "    elif cmd == 'sub':\n        return do_sub()\n"
        "    elif cmd == 'mul':\n        return do_mul()\n"
        "    elif cmd == 'div':\n        return do_div()\n"
    )
    assert _rule_lines(src, REPETITIVE) == [2]


def test_repetitive_dispatch_fires_attribute_lhs() -> None:
    src = (
        "def render(self):\n"
        "    if self.kind == 1:\n        return a()\n"
        "    elif self.kind == 2:\n        return b()\n"
        "    elif self.kind == 3:\n        return c()\n"
        "    elif self.kind == 4:\n        return d()\n"
        "    else:\n        return z()\n"
    )
    assert _fires(src, REPETITIVE)


def test_repetitive_dispatch_fires_in_membership() -> None:
    src = (
        "def classify(token):\n"
        "    if token in ('a', 'b'):\n        return 1\n"
        "    elif token in ('c', 'd'):\n        return 2\n"
        "    elif token in ('e', 'f'):\n        return 3\n"
        "    elif token in ('g', 'h'):\n        return 4\n"
    )
    assert _fires(src, REPETITIVE)


def test_repetitive_dispatch_not_fires_three_branches() -> None:
    src = (
        "def route(cmd):\n"
        "    if cmd == 'add':\n        return a()\n"
        "    elif cmd == 'sub':\n        return b()\n"
        "    elif cmd == 'mul':\n        return c()\n"
    )
    assert not _fires(src, REPETITIVE)


def test_repetitive_dispatch_not_fires_different_lhs() -> None:
    src = (
        "def check(a, b, c, d):\n"
        "    if a == 1:\n        return 1\n"
        "    elif b == 2:\n        return 2\n"
        "    elif c == 3:\n        return 3\n"
        "    elif d == 4:\n        return 4\n"
    )
    assert not _fires(src, REPETITIVE)


def test_repetitive_dispatch_not_fires_compound_conditions() -> None:
    src = (
        "def grade(score, bonus):\n"
        "    if score == 1 and bonus:\n        return 'a'\n"
        "    elif score == 2 and bonus:\n        return 'b'\n"
        "    elif score == 3 and bonus:\n        return 'c'\n"
        "    elif score == 4 and bonus:\n        return 'd'\n"
    )
    assert not _fires(src, REPETITIVE)


def test_repetitive_dispatch_generalisation_range_comparisons_not_fired() -> None:
    # Same lhs but ordered/range comparisons (<, >=) against constants are real branching
    # logic, not a dispatch table -> a naive 'same lhs + constant' match must NOT fire.
    src = (
        "def bucket(n):\n"
        "    if n < 10:\n        return 'low'\n"
        "    elif n < 20:\n        return 'mid'\n"
        "    elif n < 30:\n        return 'high'\n"
        "    elif n < 40:\n        return 'top'\n"
    )
    assert not _fires(src, REPETITIVE)


# --------------------------------------------------------------------------------------
# ai-slop/python-isinstance-ladder
# --------------------------------------------------------------------------------------

ISINSTANCE = ISINSTANCE_LADDER_SPEC.rule


def test_isinstance_ladder_fires_same_subject() -> None:
    src = (
        "def encode(value):\n"
        "    if isinstance(value, int):\n        return i(value)\n"
        "    elif isinstance(value, str):\n        return s(value)\n"
        "    elif isinstance(value, list):\n        return l(value)\n"
        "    elif isinstance(value, dict):\n        return d(value)\n"
    )
    assert _rule_lines(src, ISINSTANCE) == [2]


def test_isinstance_ladder_fires_attribute_subject() -> None:
    src = (
        "def visit(self, node):\n"
        "    if isinstance(node.value, A):\n        return 1\n"
        "    elif isinstance(node.value, B):\n        return 2\n"
        "    elif isinstance(node.value, C):\n        return 3\n"
        "    elif isinstance(node.value, D):\n        return 4\n"
        "    else:\n        return 0\n"
    )
    assert _fires(src, ISINSTANCE)


def test_isinstance_ladder_fires_tuple_type_args() -> None:
    src = (
        "def fmt(obj):\n"
        "    if isinstance(obj, (int, float)):\n        return n(obj)\n"
        "    elif isinstance(obj, str):\n        return t(obj)\n"
        "    elif isinstance(obj, bytes):\n        return b(obj)\n"
        "    elif isinstance(obj, bool):\n        return q(obj)\n"
    )
    assert _fires(src, ISINSTANCE)


def test_isinstance_ladder_not_fires_three_branches() -> None:
    src = (
        "def encode(value):\n"
        "    if isinstance(value, int):\n        return a()\n"
        "    elif isinstance(value, str):\n        return b()\n"
        "    elif isinstance(value, list):\n        return c()\n"
    )
    assert not _fires(src, ISINSTANCE)


def test_isinstance_ladder_not_fires_different_subjects() -> None:
    src = (
        "def check(a, b, c, d):\n"
        "    if isinstance(a, int):\n        return 1\n"
        "    elif isinstance(b, str):\n        return 2\n"
        "    elif isinstance(c, list):\n        return 3\n"
        "    elif isinstance(d, dict):\n        return 4\n"
    )
    assert not _fires(src, ISINSTANCE)


def test_isinstance_ladder_not_fires_compound_with_isinstance() -> None:
    src = (
        "def encode(value, flag):\n"
        "    if isinstance(value, int) and flag:\n        return a()\n"
        "    elif isinstance(value, str) and flag:\n        return b()\n"
        "    elif isinstance(value, list) and flag:\n        return c()\n"
        "    elif isinstance(value, dict) and flag:\n        return d()\n"
    )
    assert not _fires(src, ISINSTANCE)


def test_isinstance_ladder_generalisation_method_isinstance_not_fired() -> None:
    # A naive 'isinstance(value,' match would trip here, but these branches negate the check
    # (not isinstance) — a guard chain, not a type-dispatch ladder -> must NOT fire.
    src = (
        "def encode(value):\n"
        "    if not isinstance(value, int):\n        raise TypeError()\n"
        "    elif not isinstance(value, str):\n        raise TypeError()\n"
        "    elif not isinstance(value, list):\n        raise TypeError()\n"
        "    elif not isinstance(value, dict):\n        raise TypeError()\n"
    )
    assert not _fires(src, ISINSTANCE)


# --------------------------------------------------------------------------------------
# cross-rule sanity: a clean module fires nothing
# --------------------------------------------------------------------------------------


def test_clean_module_has_no_findings() -> None:
    src = "def f(items):\n" "    return [x for i, x in enumerate(items)]\n" "\n" "def g(d):\n" "    return d.get('a')\n"
    ctx = build_file_context(Path("src/clean.py"), src)
    assert detect(ctx) == []


# --- chained-dict-get: explicit default spared, absent default still flagged (tuned) ---


def test_chained_get_with_explicit_empty_dict_default_is_spared() -> None:
    # `payload.get(x, {}).get(y)` is deliberate crash-safe defensive navigation — not slop.
    assert not _fires("v = payload.get('meta', {}).get('owner')\n", "ai-slop/python-chained-dict-get")


def test_chained_get_with_any_explicit_default_is_spared() -> None:
    assert not _fires("v = cfg.get('opts', DEFAULTS).get('mode')\n", "ai-slop/python-chained-dict-get")


def test_chained_get_without_intermediate_default_still_fires() -> None:
    # `payload.get(x).get(y)` raises AttributeError when x is missing (None.get) — a real bug.
    assert _fires("v = payload.get('meta').get('owner')\n", "ai-slop/python-chained-dict-get")
