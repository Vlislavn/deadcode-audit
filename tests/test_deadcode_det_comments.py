"""Tests for the AI-slop comment detectors (scripts/deadcode/detectors/comments.py).

For each of the three rules we pin >= 3 POSITIVE cases (varied identifiers / module names, to prove
the rule encodes a *principle* not a snippet) and >= 3 ADVERSARIAL NEGATIVE cases (legitimate
near-misses that must NOT fire). Each rule also carries one explicit "generalisation" negative — a
comment a naive snippet/substring match would have flagged, but that the structural guard spares.

All findings are obtained by building a real FileContext and running ``detect`` end-to-end, so the
tokenize-driven positioning and the cross-rule ownership guards are exercised together.
"""

from __future__ import annotations

from pathlib import Path

from deadcode_audit.detectors.comments import detect
from deadcode_audit.framework import build_file_context


def _rule_lines(source: str, rule: str) -> list[int]:
    """Return the sorted 1-based lines at which ``rule`` fires for ``source``."""
    ctx = build_file_context(Path("src/x.py"), source)
    return sorted(d.line for d in detect(ctx) if d.rule == rule)


def _fires(source: str, rule: str) -> bool:
    return bool(_rule_lines(source, rule))


# =============================================================================================
# ai-slop/todo-stub
# =============================================================================================

TODO = "ai-slop/todo-stub"


def test_todo_stub_fires_on_plain_todo() -> None:
    assert _fires("x = 1  # TODO refactor this loader\n", TODO)


def test_todo_stub_fires_on_fixme_standalone() -> None:
    assert _fires("# FIXME the parser drops trailing commas\nvalue = parse()\n", TODO)


def test_todo_stub_fires_on_hack_and_xxx_varied() -> None:
    # Two distinct markers, two distinct identifiers/modules -> proves it is not one snippet.
    assert _rule_lines(
        "def serialise():\n    pass  # HACK monkeypatch the encoder\n# XXX revisit the retry budget\n",
        TODO,
    ) == [2, 3]


def test_todo_stub_spared_by_issue_reference() -> None:
    assert not _fires("queue = []  # TODO drain the queue (#4821)\n", TODO)


def test_todo_stub_spared_by_jira_key() -> None:
    assert not _fires("# FIXME flaky under load PROJ-1337\nrun()\n", TODO)


def test_todo_stub_spared_by_url() -> None:
    assert not _fires("# TODO migrate per https://example.com/issues/9\nmigrate()\n", TODO)


def test_todo_stub_spared_when_marker_used_as_noun_in_prose() -> None:
    # The marker word appears MID-sentence as a noun ('the sticky TODO widget'), not as a leading
    # directive -> prose, must not fire. This is the marker-led guard, the main noun-vs-directive fix.
    src = "widget = build()  # backs the sticky TODO widget on the surface\n"
    assert not _fires(src, TODO)


def test_todo_stub_spared_inside_multiline_comment_run() -> None:
    # A TODO leading a line that is part of a consecutive explanatory comment run is a documented
    # decision block, not an orphaned one-line stub -> spared.
    src = "# todo: always enabled because the server has no external dependency\n# and it backs the widget\nx = 1\n"
    assert not _fires(src, TODO)


def test_todo_stub_fires_when_marker_leads_after_bullet() -> None:
    # A list-bullet before the marker is still a directive ('- TODO ...') -> must fire.
    assert _fires("# - TODO wire up the retry path\nrun()\n", TODO)


def test_todo_stub_generalisation_word_boundary() -> None:
    # Naive substring 'todo'/'xxx' would match these; the word-boundary + marker-led guards must NOT
    # fire: 'mastodon' contains 'todo'; the hex literal 0xABCD / identifier 'xxxhash' contain 'xxx'.
    src = (
        "client = mastodon_client()  # connect to the mastodon api\n" "mask = 0xABCD  # bit mask for the xxxhash seed\n"
    )
    assert not _fires(src, TODO)


# =============================================================================================
# ai-slop/trivial-comment
# =============================================================================================

TRIVIAL = "ai-slop/trivial-comment"


def test_trivial_comment_restates_import() -> None:
    assert _fires("# import os\nimport os\n", TRIVIAL)


def test_trivial_comment_restates_return_inline() -> None:
    assert _fires("def f(payload):\n    return payload  # return payload\n", TRIVIAL)


def test_trivial_comment_restates_increment_via_identifier() -> None:
    # '# increment counter' over 'counter += 1' — the AST AugAssign+Add operation paraphrases to
    # {increment, add, ...}, so the comment reduces entirely to the line's vocabulary. Fires.
    assert _fires("# increment counter\ncounter += 1\n", TRIVIAL)


def test_trivial_comment_augop_verb_generalises_across_operators() -> None:
    # The operation->verb mapping is structural, not a snippet: a DIFFERENT operator (-=, with a
    # different identifier) and a different verb ('decrement') must also reduce identically and fire.
    assert _fires("# decrement remaining\nremaining -= step\n", TRIVIAL)


def test_trivial_comment_spared_when_labelling_a_block() -> None:
    # A standalone comment above a block-opening statement LABELS the block, it does not restate one
    # operation -> spared (the '# Dependencies' over 'if ...:' false positive from real source).
    assert not _fires("# dependencies\nif dependencies:\n    pass\n", TRIVIAL)


def test_trivial_comment_spared_when_separator_framed_heading() -> None:
    # A heading framed by separators is a section header (narrative's domain), not a trivial restate.
    assert not _fires("# ==========\n# main\n# ==========\ndef main():\n    pass\n", TRIVIAL)


def test_trivial_comment_spared_when_adds_why() -> None:
    # Restates the call but ALSO gives a reason -> the 'why' guard spares it.
    assert not _fires("retry()  # retry because the upstream is flaky under load\n", TRIVIAL)


def test_trivial_comment_spared_when_adds_information() -> None:
    # Comment mentions a unit/constraint absent from the code -> not a subset -> not flagged.
    assert not _fires("timeout = 30  # timeout in seconds, tuned for the slow mirror\n", TRIVIAL)


def test_trivial_comment_spared_for_type_and_noqa_directives() -> None:
    assert not _fires("config = load()  # type: ignore\nrows = []  # noqa: E501\n", TRIVIAL)


def test_trivial_comment_generalisation_not_substring() -> None:
    # A naive 'comment words appear in the file' match would flag this: every comment word DOES
    # appear elsewhere in the code, but NOT on the adjacent line, so the structural guard spares it.
    src = "def export(rows):\n    # serialise the rows into csv and stream them\n    return write(rows)\n"
    assert not _fires(src, TRIVIAL)


def test_trivial_comment_spared_for_todo_marker() -> None:
    # Owned by todo-stub, never double-reported by trivial-comment even though it sits over a return.
    assert not _fires("def f(x):\n    # TODO return x\n    return x\n", TRIVIAL)


# =============================================================================================
# ai-slop/narrative-comment
# =============================================================================================

NARRATIVE = "ai-slop/narrative-comment"


def test_narrative_fires_on_equals_separator() -> None:
    assert _fires("# ==================\nrun()\n", NARRATIVE)


def test_narrative_fires_on_dash_and_star_separators() -> None:
    assert _rule_lines("# ----------\nx = 1\n# ********\ny = 2\n", NARRATIVE) == [1, 3]


def test_narrative_spares_step_phase_section_headers() -> None:
    # Tuned: section/phase headers carry a navigation label, not comment art -> spared.
    src = "# Step 1: load the model\n# Phase 2 - warm the cache\n# === Section ===\n"
    assert _rule_lines(src, NARRATIVE) == []


def test_narrative_spared_on_short_prose() -> None:
    assert not _fires("# guard against the empty batch\nprocess(batch)\n", NARRATIVE)


def test_narrative_spared_on_why_prose_with_dashes_inline() -> None:
    # An em-dash style explanation is prose, not a separator (fewer than 6 repeated punct chars).
    assert not _fires("# we retry here - the broker drops the first frame\nretry()\n", NARRATIVE)


def test_narrative_spared_on_short_punctuation_run() -> None:
    # Five dashes is below the >= 6 separator threshold -> not flagged (conservative boundary).
    assert not _fires("# -----\nx = 1\n", NARRATIVE)


def test_narrative_spared_on_directives() -> None:
    assert not _fires("#!/usr/bin/env python\n# -*- coding: utf-8 -*-\nx = 1\n", NARRATIVE)


def test_narrative_generalisation_step_in_prose() -> None:
    # Naive 'contains the word step' would flag this; the header shape requires 'Step <N>:' so a
    # sentence merely mentioning a step is spared.
    assert not _fires("# the next step depends on whether the cache is warm\nstep()\n", NARRATIVE)


def test_narrative_spared_on_prose_starting_with_section_word() -> None:
    # A header WORD at the start without an ordinal+delimiter is prose, not a header: 'Section / phase
    # header shapes ...' and 'Part of the pipeline ...' must NOT fire (the bare-word over-match guard).
    assert not _fires("# Section and phase headers are detected by their shape\nx = 1\n", NARRATIVE)
    assert not _fires("# Part of the request body is validated downstream\ny = 2\n", NARRATIVE)


# =============================================================================================
# cross-rule / structural sanity
# =============================================================================================


def test_string_and_docstring_content_never_self_matches() -> None:
    # A TODO/separator/trivial pattern living INSIDE a string or docstring must not be tokenised as
    # a comment, so none of the rules fire on these lines.
    src = (
        'DOC = """\n' "# TODO this is inside a docstring\n" "# ==========\n" '"""\n' "message = '# import os'  # noqa\n"
    )
    ctx = build_file_context(Path("src/y.py"), src)
    rules_fired = {d.rule for d in detect(ctx)}
    assert TODO not in rules_fired
    assert NARRATIVE not in rules_fired
    assert TRIVIAL not in rules_fired


def test_all_rule_ids_have_specs() -> None:
    # Every rule id emitted by detect must be backed by a declared RuleSpec.
    from deadcode_audit.detectors.comments import rules

    spec_ids = {spec.rule for spec in rules}
    assert spec_ids == {TODO, TRIVIAL, NARRATIVE}


# --- narrative-comment: labeled section dividers spared, lone rules still flagged (tuned) ---


def test_narrative_separator_framing_a_label_is_spared() -> None:
    src = "# =====================\n# Routing & Intent\n# =====================\nrows = 1\n"
    assert not _fires(src, "ai-slop/narrative-comment")


def test_narrative_lone_separator_still_fires() -> None:
    src = "alpha = 1\n# =====================\nbeta = 2\n"
    assert _fires(src, "ai-slop/narrative-comment")
