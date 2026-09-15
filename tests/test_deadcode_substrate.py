"""Tests for the deadcode scan substrate: scoring, masker, config, output, history, framework."""

from pathlib import Path

import pytest

from deadcode_audit import _mask, config, framework, history, output
from deadcode_audit.diagnostic import ENGINE_AI_SLOP, Diagnostic, RuleSpec, Severity, diagnostic_from_spec
from deadcode_audit.scoring import calculate_score


def _diag(
    rule: str, severity: Severity = Severity.WARNING, engine: str = ENGINE_AI_SLOP, file: str = "src/a.py"
) -> Diagnostic:
    return Diagnostic(file_path=file, engine=engine, rule=rule, severity=severity, message="m", line=1)


# --- scoring ---


def test_score_empty_is_perfect() -> None:
    assert calculate_score([]).score == 100


def test_score_density_softens_large_repos() -> None:
    d = [_diag("ai-slop/swallowed-exception", Severity.ERROR)]
    assert calculate_score(d, source_file_count=500).score > calculate_score(d, source_file_count=1).score


def test_score_style_rules_count_half() -> None:
    style = calculate_score([_diag("ai-slop/narrative-comment")], source_file_count=10).score
    real = calculate_score([_diag("ai-slop/swallowed-exception")], source_file_count=10).score
    assert style >= real  # style penalised less -> higher score


def test_score_more_errors_lower_score() -> None:
    one = calculate_score([_diag("r/x", Severity.ERROR)], source_file_count=10).score
    many = calculate_score(
        [_diag(f"r/x{i}", Severity.ERROR, file=f"src/f{i}.py") for i in range(8)], source_file_count=10
    ).score
    assert many < one


def test_score_labels() -> None:
    assert calculate_score([]).label == "Healthy"


# --- masker ---


def test_mask_preserves_offsets_and_blanks_content() -> None:
    src = 'url = "http://evil.example"  # TODO leak\nok = 1\n'
    masked = _mask.mask_strings_and_comments(src)
    assert len(masked) == len(src)
    assert masked.count("\n") == src.count("\n")
    assert "TODO" not in masked and "evil" not in masked
    assert "url = " in masked and "ok = 1" in masked  # code preserved


# --- config ---


def test_config_absent_returns_defaults(tmp_path: Path) -> None:
    cfg = config.load_deadcode_config(tmp_path)
    assert cfg.rule_severity == {} and cfg.fail_below is None


def test_config_unquoted_off_is_accepted(tmp_path: Path) -> None:
    (tmp_path / ".deadcode.yml").write_text("rules:\n  ai-slop/x: off\n", encoding="utf-8")
    cfg = config.load_deadcode_config(tmp_path)
    assert cfg.rule_severity == {"ai-slop/x": "off"}


def test_apply_rule_severities_drops_off_and_rewrites() -> None:
    diags = [_diag("ai-slop/x"), _diag("ai-slop/y")]
    out = config.apply_rule_severities(diags, {"ai-slop/x": "off", "ai-slop/y": "error"})
    assert [(d.rule, d.severity) for d in out] == [("ai-slop/y", Severity.ERROR)]


def test_config_extends_merges_parent(tmp_path: Path) -> None:
    (tmp_path / "base.yml").write_text("scoring:\n  smoothing: 30\nci:\n  failBelow: 60\n", encoding="utf-8")
    (tmp_path / ".deadcode.yml").write_text("extends: base.yml\nci:\n  failBelow: 80\n", encoding="utf-8")
    cfg = config.load_deadcode_config(tmp_path)
    assert cfg.smoothing == 30 and cfg.fail_below == 80  # parent smoothing kept, child failBelow wins


def test_config_fail_closed_on_bad_severity(tmp_path: Path) -> None:
    (tmp_path / ".deadcode.yml").write_text("rules:\n  r: loud\n", encoding="utf-8")
    with pytest.raises(ValueError, match="severity must be one of"):
        config.load_deadcode_config(tmp_path)


def test_config_fail_closed_on_unknown_key(tmp_path: Path) -> None:
    (tmp_path / ".deadcode.yml").write_text("bogus: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown top-level"):
        config.load_deadcode_config(tmp_path)


# --- output ---


def test_render_json_carries_score_and_diagnostics() -> None:
    import json

    payload = json.loads(
        output.render_json(
            [_diag("ai-slop/x", Severity.ERROR)], calculate_score([_diag("ai-slop/x")], source_file_count=5)
        )
    )
    assert payload["score"] <= 100 and len(payload["diagnostics"]) == 1 and payload["summary"]["errors"] == 1


def test_render_sarif_is_2_1_0_with_dedup_rules() -> None:
    import json

    diags = [_diag("ai-slop/x"), _diag("ai-slop/x", file="src/b.py"), _diag("ai-slop/y")]
    sarif = json.loads(output.render_sarif(diags))
    assert sarif["version"] == "2.1.0"
    rules = sarif["runs"][0]["tool"]["driver"]["rules"]
    assert len(rules) == 2  # de-duplicated by rule id
    assert sarif["runs"][0]["results"][0]["level"] in {"error", "warning", "note"}


def test_render_terminal_and_prompt() -> None:
    sc = calculate_score([_diag("ai-slop/x")], source_file_count=5)
    assert "score:" in output.render_terminal([_diag("ai-slop/x")], sc)
    assert "ai-slop/x" in output.render_agent_prompt([_diag("ai-slop/x")])
    assert "no findings" in output.render_terminal([], calculate_score([]))


# --- history ---


def test_history_round_trip_and_sparkline(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    rec = history.HistoryRecord(
        timestamp="2026-06-02T00:00:00+00:00", score=88, label="Healthy", errors=0, warnings=2, info=1, files=10
    )
    history.append_record(rec, path)
    history.append_record(history.HistoryRecord("2026-06-02T01:00:00+00:00", 72, "Needs Work", 1, 3, 0, 11), path)
    records = history.read_records(path)
    assert [r.score for r in records] == [88, 72]
    assert len(history.sparkline([0, 50, 100])) == 3
    assert "no score history" in history.render_trend([])
    assert "88" in history.render_trend(records)


# --- framework ---

_SPEC = RuleSpec(rule="test/x", engine=ENGINE_AI_SLOP, default_severity=Severity.INFO, category="Test", help="fix it")


class _DummyDetector:
    rules = (_SPEC,)

    def detect(self, ctx: framework.FileContext) -> list[Diagnostic]:
        return [diagnostic_from_spec(_SPEC, file_path=ctx.path.as_posix(), line=1, message="found")]


def test_run_detectors_aggregates() -> None:
    out = framework.run_detectors([(Path("src/a.py"), "x = 1\n")], [_DummyDetector()])
    assert [d.rule for d in out] == ["test/x"]


def test_run_detectors_surfaces_parse_error_not_crash() -> None:
    out = framework.run_detectors([(Path("src/bad.py"), "def (:\n")], [_DummyDetector()])
    assert any(d.rule == "deadcode/parse-error" and d.severity is Severity.ERROR for d in out)


def test_all_rule_specs_includes_parse_error_and_detector_rules() -> None:
    specs = {s.rule for s in framework.all_rule_specs([_DummyDetector()])}
    assert "test/x" in specs and "deadcode/parse-error" in specs


def test_masked_source_is_lazy_and_cached() -> None:
    ctx = framework.build_file_context(Path("src/a.py"), 's = "secret"\n')
    assert "secret" not in ctx.masked_source
    assert ctx.masked_source is ctx.masked_source  # cached
