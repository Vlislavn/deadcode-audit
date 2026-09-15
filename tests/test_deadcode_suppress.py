"""Tests for inline scan suppression (scripts/deadcode/suppress.py).

Covers directive parsing + filtering: bare line ignore (all rules), coded line ignore (short code AND
full rule id), non-matching code is NOT suppressed, file-level ignore-file, and a directive written
inside a string literal must NOT take effect (tokenised, not regex-on-source).
"""

from __future__ import annotations

from deadcode_audit import suppress
from deadcode_audit.diagnostic import ENGINE_AI_SLOP, Diagnostic, Severity


def _diag(rule: str, line: int) -> Diagnostic:
    return Diagnostic(
        file_path="src/x.py",
        engine=ENGINE_AI_SLOP,
        rule=rule,
        severity=Severity.WARNING,
        message="m",
        line=line,
    )


def test_bare_line_ignore_suppresses_every_rule_on_that_line() -> None:
    source = "x = thing()  # ai-slop: ignore\n"
    diags = [_diag("ai-slop/swallowed-exception", 1), _diag("ai-slop/thin-wrapper", 1)]
    assert suppress.apply_suppressions(diags, source) == []


def test_coded_line_ignore_short_segment_matches() -> None:
    source = "except Exception:  # ai-slop: ignore[swallowed-exception]\n"
    kept = suppress.apply_suppressions([_diag("ai-slop/swallowed-exception", 1)], source)
    assert kept == []


def test_coded_line_ignore_full_rule_id_matches() -> None:
    source = "except Exception:  # ai-slop: ignore[ai-slop/swallowed-exception]\n"
    assert suppress.apply_suppressions([_diag("ai-slop/swallowed-exception", 1)], source) == []


def test_coded_line_ignore_does_not_suppress_other_rules() -> None:
    source = "block  # ai-slop: ignore[swallowed-exception]\n"
    diag = _diag("ai-slop/thin-wrapper", 1)
    assert suppress.apply_suppressions([diag], source) == [diag]  # different rule -> kept


def test_line_directive_only_affects_its_own_line() -> None:
    source = "a = 1  # ai-slop: ignore\nb = 2\n"
    diag = _diag("ai-slop/thin-wrapper", 2)  # finding on line 2, directive on line 1
    assert suppress.apply_suppressions([diag], source) == [diag]


def test_file_level_ignore_suppresses_matching_rule_anywhere() -> None:
    source = '"""doc."""\n# ai-slop: ignore-file[file-too-large]\nx = 1\n'
    # file-too-large is reported on line 1 (the docstring) — only a file-level directive can reach it.
    assert suppress.apply_suppressions([_diag("code-quality/file-too-large", 1)], source) == []


def test_file_level_coded_ignore_does_not_suppress_other_rules() -> None:
    source = "# ai-slop: ignore-file[file-too-large]\nx = 1\n"
    diag = _diag("ai-slop/swallowed-exception", 5)
    assert suppress.apply_suppressions([diag], source) == [diag]


def test_directive_inside_string_literal_does_not_suppress() -> None:
    # The text lives in a string, not a comment -> tokenize never sees it as a directive.
    source = 'note = "# ai-slop: ignore[swallowed-exception]"\n'
    diag = _diag("ai-slop/swallowed-exception", 1)
    assert suppress.apply_suppressions([diag], source) == [diag]


def test_no_directives_returns_input_unchanged() -> None:
    source = "x = 1\ny = 2\n"
    diags = [_diag("ai-slop/thin-wrapper", 1)]
    assert suppress.apply_suppressions(diags, source) == diags
