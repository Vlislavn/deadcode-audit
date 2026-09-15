"""Tests for the redundancy / near-duplicate linter.

Positives include patterns BEYOND the motivating examples (idempotent self-application,
round-trips, identity comprehensions/maps) to prove the rules generalise; negatives are
adversarial near-misses that must NOT fire (de-dup, ordering, filtering, transforms).
"""

from pathlib import Path

import pytest

from deadcode_audit.clones import collect_functions, find_near_duplicate_functions
from deadcode_audit.redundancy import find_redundant_transforms, run_files


def _kinds(src: str) -> list[str]:
    return [f.kind for f in find_redundant_transforms(f"value = {src}\n", Path("m.py"))]


REDUNDANT = [
    # idempotent self-application (now driven by IDEMPOTENT_CALLABLES, with a simple-call guard)
    "sorted(sorted(x))",
    "set(set(x))",
    "frozenset(frozenset(x))",
    "abs(abs(x))",  # was MISSED before the dead IDEMPOTENT_CALLABLES path was activated
    "bool(bool(x))",
    "str(str(x))",
    "int(int(x))",
    "float(float(x))",
    "round(round(x))",
    # redundant re-construction
    "list(list(x))",
    "tuple(tuple(x))",
    "dict(dict(x))",
    # round-trip conversions (dict<->list among them — a named example)
    "list(tuple(x))",
    "tuple(list(x))",
    "dict(list(d.items()))",
    "dict(tuple(pairs))",
    # unordered-outer makes inner ordering/listing pointless
    "set(list(x))",
    "set(sorted(x))",
    "frozenset(list(x))",
    "frozenset(sorted(x))",
    # sorting an already-materialised iterable
    "sorted(list(x))",
    "sorted(tuple(x))",
]

NOT_REDUNDANT = [
    "list(set(x))",  # de-duplicates — order/content changes
    "list(sorted(x))",  # conservatively not flagged (only a wasted copy, not an order/type change)
    "dict(sorted(items))",  # insertion order is meaningful in dict
    "set(x)",
    "sorted(x)",
    "sorted(x, key=k)",
    "dict(d)",
    "tuple(x)",
    # CRITICAL: the multi-key stable-sort idiom is NOT redundant — the inner sort is a tiebreak.
    "sorted(sorted(rows, key=by_name), key=by_dept)",
    "sorted(sorted(x), key=k)",
    "sorted(sorted(x, key=k))",
    # extra args make the self-application non-idempotent / additive
    "dict(dict(x), extra=1)",
    "round(round(x, 2), 2)",
    "int(int(s, 2))",
    # mixed coercions are real transforms, not self-application
    "str(int(x))",
    "int(str(x))",
    "float(int(x))",
]


@pytest.mark.parametrize("src", REDUNDANT)
def test_redundant_compositions_are_flagged(src: str) -> None:
    assert "collapsible-composition" in _kinds(src), src


@pytest.mark.parametrize("src", NOT_REDUNDANT)
def test_non_redundant_compositions_are_not_flagged(src: str) -> None:
    assert _kinds(src) == [], src


IDENTITY_COMPS = [
    "[x for x in y]",
    "{x for x in y}",
    "(x for x in y)",
    "{k: v for k, v in d.items()}",
]

NON_IDENTITY_COMPS = [
    "[f(x) for x in y]",  # transforms the element
    "[x for x in y if x]",  # filters
    "[x for x in y for z in w]",  # nested
    "{v: k for k, v in d.items()}",  # swaps key/value
    "{k: f(v) for k, v in d.items()}",  # transforms value
    "[x.name for x in y]",  # attribute access, not identity
]


@pytest.mark.parametrize("src", IDENTITY_COMPS)
def test_identity_comprehensions_are_flagged(src: str) -> None:
    assert "identity-comprehension" in _kinds(src), src


@pytest.mark.parametrize("src", NON_IDENTITY_COMPS)
def test_non_identity_comprehensions_are_not_flagged(src: str) -> None:
    assert "identity-comprehension" not in _kinds(src), src


@pytest.mark.parametrize("src", ["list(map(lambda a: a, y))", "map(lambda z: z, y)", "set(map(lambda v: v, y))"])
def test_identity_maps_are_flagged(src: str) -> None:
    assert "identity-map" in _kinds(src), src


@pytest.mark.parametrize("src", ["list(map(str, y))", "map(lambda a: a + 1, y)", "list(map(lambda a, b: a, y, z))"])
def test_non_identity_maps_are_not_flagged(src: str) -> None:
    assert "identity-map" not in _kinds(src), src


@pytest.mark.parametrize("src", ["-(-x)", "~~x", "+(+x)"])
def test_involutions_are_flagged(src: str) -> None:
    assert "involution" in _kinds(src), src


@pytest.mark.parametrize("src", ["not not x", "-x", "~x", "+x", "-(~x)", "not x"])
def test_non_involutions_are_not_flagged(src: str) -> None:
    # `not not x` coerces to bool (a real transform); mixed/single ops are not no-ops.
    assert "involution" not in _kinds(src), src


def test_redundant_finding_carries_line_number() -> None:
    src = "a = 1\nb = sorted(sorted(items))\n"
    findings = find_redundant_transforms(src, Path("m.py"))
    assert findings[0].line == 2


# --- clone detection ---

_ALPHA = """
def alpha(items):
    total = 0
    for it in items:
        if it.active:
            total += it.value * 2
    return total
"""
_BETA_RENAMED = """
def beta(rows):
    acc = 0
    for r in rows:
        if r.active:
            acc += r.value * 2
    return acc
"""
_GAMMA_DIFFERENT = """
def gamma(text):
    cleaned = text.strip().lower()
    return cleaned.replace(' ', '_')
"""


def test_renamed_near_duplicate_function_is_detected() -> None:
    recs = collect_functions(_ALPHA, Path("a.py")) + collect_functions(_BETA_RENAMED, Path("b.py"))
    clones = find_near_duplicate_functions(recs, recs, threshold=0.9, min_tokens=5)
    pairs = {tuple(sorted((c.symbol, c.other_symbol))) for c in clones}
    assert ("alpha", "beta") in pairs


def test_structurally_different_functions_are_not_flagged() -> None:
    recs = collect_functions(_ALPHA, Path("a.py")) + collect_functions(_GAMMA_DIFFERENT, Path("g.py"))
    clones = find_near_duplicate_functions(recs, recs, threshold=0.9, min_tokens=5)
    assert all("gamma" not in (c.symbol, c.other_symbol) for c in clones)


def test_same_skeleton_but_disjoint_api_is_not_a_clone() -> None:
    """Two functions sharing only a control-flow skeleton (disjoint calls/attrs) must NOT match."""
    a = """
def fetch_user(client, uid):
    response = client.get_user(uid)
    if response.ok:
        return response.profile.normalize()
    return None
"""
    b = """
def parse_feed(reader, url):
    entry = reader.load_feed(url)
    if entry.valid:
        return entry.payload.serialize()
    return None
"""
    recs = collect_functions(a, Path("a.py")) + collect_functions(b, Path("b.py"))
    clones = find_near_duplicate_functions(recs, recs, threshold=0.85, min_tokens=5, semantic_threshold=0.6)
    assert clones == []


def test_run_files_blocks_on_redundant_transform(tmp_path: Path) -> None:
    """Pre-commit file mode: a file with a reducible transform exits non-zero."""
    bad = tmp_path / "bad.py"
    bad.write_text("def f(x):\n    return sorted(sorted(x))\n", encoding="utf-8")
    assert run_files([str(bad)]) == 1


def test_run_files_passes_clean_file(tmp_path: Path) -> None:
    """Pre-commit file mode: a clean file (and the multi-key sort idiom) exits zero."""
    good = tmp_path / "good.py"
    good.write_text(
        "def f(rows):\n    return sorted(sorted(rows, key=str), key=len)\n",
        encoding="utf-8",
    )
    assert run_files([str(good)]) == 0


def test_run_files_ignores_non_python_paths(tmp_path: Path) -> None:
    """Non-.py paths pre-commit might pass are skipped, not errored on."""
    note = tmp_path / "note.md"
    note.write_text("sorted(sorted(x))\n", encoding="utf-8")
    assert run_files([str(note)]) == 0


def test_trivially_small_functions_are_skipped() -> None:
    src_a = "def f():\n    return 1\n"
    src_b = "def g():\n    return 2\n"
    recs = collect_functions(src_a, Path("a.py")) + collect_functions(src_b, Path("b.py"))
    assert find_near_duplicate_functions(recs, recs, threshold=0.9, min_tokens=40) == []
