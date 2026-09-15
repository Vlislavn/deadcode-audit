"""Tests for the complexity detector (scripts/deadcode/detectors/complexity.py).

For each rule: >=3 positive cases (varied identifiers, proving the principle generalises) and >=3
adversarial negatives (legitimate near-misses that must NOT fire), plus an explicit "generalisation"
negative that a naive snippet/threshold match would have tripped on. Tests build a real FileContext
via build_file_context and assert on the rule ids returned by detect(ctx).
"""

from pathlib import Path

from deadcode_audit.detectors import complexity
from deadcode_audit.framework import build_file_context


def _rule_ids(source: str) -> list[str]:
    ctx = build_file_context(Path("src/x.py"), source)
    return [d.rule for d in complexity.detect(ctx)]


def _details_for(source: str, rule: str) -> list[str | None]:
    ctx = build_file_context(Path("src/x.py"), source)
    return [d.detail for d in complexity.detect(ctx) if d.rule == rule]


# --------------------------------------------------------------------------------------
# code-quality/function-too-long
# --------------------------------------------------------------------------------------

_LONG = "code-quality/function-too-long"


def _body(n: int, *, indent: str = "    ", var: str = "total") -> str:
    return "\n".join(f"{indent}{var} = {var} + {i}" for i in range(n))


def test_long_function_fires_plain() -> None:
    src = "def compute_score():\n" + _body(90, var="acc")
    assert _LONG in _rule_ids(src)


def test_long_async_function_fires() -> None:
    src = "async def gather_rows(client):\n" + _body(85, var="rows")
    assert _LONG in _rule_ids(src)


def test_long_method_with_multiline_signature_fires() -> None:
    sig = "class Pipeline:\n    def run(\n        self,\n        source,\n        sink,\n    ):\n"
    src = sig + _body(90, indent="        ", var="state")
    assert _LONG in _rule_ids(src)


def test_long_function_detail_counts_logical_lines_only() -> None:
    # 81 codeful lines (> 80) interspersed with blanks/comments that must NOT inflate the count.
    code_lines = "\n".join(f"    step_{i} = step_{i} + 1" for i in range(81))
    src = 'def orchestrate():\n    """Docstring line one.\n    line two.\n    """\n    # a comment\n\n' + code_lines
    details = _details_for(src, _LONG)
    assert details == ["81 lines"]


def test_short_function_does_not_fire() -> None:
    src = "def tiny(a, b):\n    return a + b\n"
    assert _LONG not in _rule_ids(src)


def test_well_documented_short_function_does_not_fire() -> None:
    # Huge docstring + blanks but only a few real lines -> must NOT fire (docstring/blanks excluded).
    doc = "\n".join(f"    explanation line {i}." for i in range(120))
    src = f'def documented():\n    """\n{doc}\n    """\n    return 42\n'
    assert _LONG not in _rule_ids(src)


def test_comment_heavy_short_function_does_not_fire() -> None:
    comments = "\n".join(f"    # narration {i}" for i in range(120))
    src = "def annotated():\n" + comments + "\n    return 1\n"
    assert _LONG not in _rule_ids(src)


def test_function_at_threshold_does_not_fire() -> None:
    # Exactly 80 logical lines -> at the cap, NOT over it (boundary, false-negative bias).
    src = "def boundary():\n" + _body(80, var="x")
    assert _LONG not in _rule_ids(src)


def test_nested_helper_is_measured_independently() -> None:
    # An over-long nested helper is reported on its own; a short outer wrapper around a short
    # helper stays clean (each scope measured independently). The helper here is the only long one.
    helper = "\n".join(f"        acc = acc + {i}" for i in range(90))
    src = "def outer():\n    def inner():\n" + helper + "\n    return inner\n"
    ids = _rule_ids(src)
    # inner is over-long; the rule fires (at least once). The detail proves logical counting.
    assert _LONG in ids
    assert "90 lines" in _details_for(src, _LONG)


def test_short_outer_around_short_helper_does_not_fire() -> None:
    src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    assert _LONG not in _rule_ids(src)


def test_generalisation_hash_inside_string_is_not_a_comment() -> None:
    # A line that ends in a '#'-containing STRING must still count as code (masking prevents
    # mis-classifying it as a comment-only line). 81 such lines => fires; proves we don't naively
    # treat any line containing '#' as a comment.
    code_lines = "\n".join(f'    label_{i} = "color #{i}"' for i in range(81))
    src = "def paint():\n" + code_lines
    assert _LONG in _rule_ids(src)


# --------------------------------------------------------------------------------------
# code-quality/file-too-large
# --------------------------------------------------------------------------------------

_FILE = "code-quality/file-too-large"


def test_large_file_fires() -> None:
    src = "\n".join(f"VALUE_{i} = {i}" for i in range(550))  # > 500 (the project file-size limit)
    ids = _rule_ids(src)
    assert ids.count(_FILE) == 1  # emitted exactly once


def test_large_file_of_blank_lines_still_fires() -> None:
    # Total *physical* line count is the principle, regardless of content.
    src = "x = 1\n" + ("\n" * 520)
    assert _FILE in _rule_ids(src)


def test_large_file_detail_is_total_lines() -> None:
    src = "\n".join(f"row_{i} = {i}" for i in range(600))
    assert _details_for(src, _FILE) == ["600 lines"]


def test_small_file_does_not_fire() -> None:
    src = "\n".join(f"K_{i} = {i}" for i in range(50))
    assert _FILE not in _rule_ids(src)


def test_file_at_threshold_does_not_fire() -> None:
    # Threshold is FILE_MAX_LINES (500), aligned to the source monorepo's own file-size gate; exactly 500 must NOT fire.
    src = "\n".join(f"N_{i} = {i}" for i in range(complexity.FILE_MAX_LINES))
    assert _FILE not in _rule_ids(src)


def test_file_just_under_threshold_does_not_fire() -> None:
    src = "\n".join(f"M_{i} = {i}" for i in range(complexity.FILE_MAX_LINES - 1))
    assert _FILE not in _rule_ids(src)


# --------------------------------------------------------------------------------------
# code-quality/deep-nesting
# --------------------------------------------------------------------------------------

_NEST = "code-quality/deep-nesting"


def test_deep_if_chain_fires() -> None:
    src = (
        "def walk(grid):\n"
        "    if a:\n"
        "        for row in grid:\n"
        "            while row:\n"
        "                with lock:\n"
        "                    if row.ok:\n"
        "                        if row.ready:\n"
        "                            return row\n"
    )
    assert _NEST in _rule_ids(src)


def test_deep_nesting_via_try_and_for_fires() -> None:
    src = (
        "def parse(items):\n"
        "    for item in items:\n"
        "        try:\n"
        "            if item:\n"
        "                while item.next:\n"
        "                    with item.ctx:\n"
        "                        return item\n"
        "        except ValueError:\n"
        "            raise\n"
    )
    assert _NEST in _rule_ids(src)


def test_deep_nesting_inside_nested_helper_fires() -> None:
    # A nested function with its own deep arrow shape must still be flagged (each scope analysed).
    src = (
        "def outer(data):\n"
        "    def inner():\n"
        "        if p:\n"
        "            for q in data:\n"
        "                while q:\n"
        "                    with q:\n"
        "                        if q.v:\n"
        "                            if q.w:\n"
        "                                return q\n"
        "    return inner\n"
    )
    assert _NEST in _rule_ids(src)


def test_flat_function_with_many_siblings_does_not_fire() -> None:
    # The key guard: many sibling statements at shallow depth is NOT deep nesting.
    body = "\n".join(f"    field_{i} = compute(field_{i})" for i in range(60))
    src = "def configure():\n" + body + "\n"
    assert _NEST not in _rule_ids(src)


def test_moderate_nesting_does_not_fire() -> None:
    src = (
        "def handle(req):\n"
        "    if req:\n"
        "        for h in req.headers:\n"
        "            while h:\n"
        "                return h\n"
    )
    assert _NEST not in _rule_ids(src)


def test_many_flat_branches_does_not_fire() -> None:
    # A long if/elif ladder stays shallow (each elif is a sibling, not deeper).
    branches = "\n".join(f"    elif kind == {i}:\n        return {i}" for i in range(40))
    src = "def classify(kind):\n    if kind == 0:\n        return 0\n" + branches + "\n"
    assert _NEST not in _rule_ids(src)


def test_nesting_at_threshold_does_not_fire() -> None:
    # Exactly depth 5 -> at the cap, not over it.
    src = (
        "def edge(grid):\n"
        "    if a:\n"
        "        for r in grid:\n"
        "            while r:\n"
        "                with r:\n"
        "                    return r\n"
    )
    assert _NEST not in _rule_ids(src)


def test_explicit_else_then_if_is_real_nesting_and_fires() -> None:
    # An explicit `else:` whose body is an indented `if` IS one level deeper (unlike an `elif`).
    # depth: if(1) for(2) while(3) with(4) else(2)->if(3)->if(4)->if(5)->if(6) => fires.
    src = (
        "def split(grid):\n"
        "    if a:\n"
        "        for r in grid:\n"
        "            while r:\n"
        "                with r:\n"
        "                    return r\n"
        "    else:\n"
        "        if p:\n"
        "            if q:\n"
        "                if s:\n"
        "                    if t:\n"
        "                        return p\n"
    )
    assert _NEST in _rule_ids(src)


def test_long_elif_ladder_then_real_nesting_still_fires() -> None:
    # The elif chain stays flat, but a genuinely deep block inside one branch must still be caught
    # (proves the elif guard doesn't blanket-suppress real nesting in the chain).
    deep_branch = (
        "    elif kind == 1:\n"
        "        for x in kind:\n"
        "            while x:\n"
        "                with x:\n"
        "                    if x.a:\n"
        "                        if x.b:\n"
        "                            return x\n"
    )
    src = "def route(kind):\n    if kind == 0:\n        return 0\n" + deep_branch
    assert _NEST in _rule_ids(src)


# --------------------------------------------------------------------------------------
# code-quality/too-many-params
# --------------------------------------------------------------------------------------

_PARAMS = "code-quality/too-many-params"


def test_too_many_required_params_fires() -> None:
    src = "def build(a, b, c, d, e, f, g):\n    return 0\n"
    assert _PARAMS in _rule_ids(src)


def test_too_many_params_method_excludes_self() -> None:
    # self is excluded, so 7 real params after self must fire (proves self isn't double-counted away).
    src = "class S:\n    def run(self, p1, p2, p3, p4, p5, p6, p7):\n        return p1\n"
    assert _PARAMS in _rule_ids(src)


def test_too_many_keyword_only_required_params_fires() -> None:
    src = "def cfg(*, alpha, beta, gamma, delta, epsilon, zeta, eta):\n    return 0\n"
    assert _PARAMS in _rule_ids(src)


def test_too_many_params_detail_is_required_count() -> None:
    src = "def make(one, two, three, four, five, six, seven, eight):\n    return 0\n"
    assert _details_for(src, _PARAMS) == ["8 params"]


def test_seven_params_with_defaults_does_not_fire() -> None:
    # Only 6 are required; the seventh has a default -> not counted.
    src = "def opts(a, b, c, d, e, f, g=1):\n    return 0\n"
    assert _PARAMS not in _rule_ids(src)


def test_six_required_params_at_threshold_does_not_fire() -> None:
    src = "def six(a, b, c, d, e, f):\n    return 0\n"
    assert _PARAMS not in _rule_ids(src)


def test_args_kwargs_do_not_count() -> None:
    # *args/**kwargs and the bare self are not required params; only 5 real ones here.
    src = "def variadic(self, a, b, c, d, e, *args, **kwargs):\n    return 0\n"
    assert _PARAMS not in _rule_ids(src)


def test_generalisation_many_defaulted_params_does_not_fire() -> None:
    # 10 parameters but all 8 extra ones default -> a naive "len(args) > 6" match would fire; we must not.
    src = "def configurable(host, port, *, retries=3, timeout=5, backoff=1, verbose=False, tag='', mode='r', cap=10, tee=None):\n    return 0\n"
    assert _PARAMS not in _rule_ids(src)


def test_positional_only_required_params_fire() -> None:
    src = "def posonly(a, b, c, d, e, f, g, /):\n    return 0\n"
    assert _PARAMS in _rule_ids(src)
