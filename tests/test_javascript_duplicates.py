"""Optional pinned jscpd adapter: cluster collapsing, skip policy, test-file exclusion, real-tool e2e."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from deadcode_audit import layout
from deadcode_audit.js import javascript

CANNED = Path(__file__).parent / "fixtures" / "jscpd_report.json"
FAKE_JSCPD = """#!{python}
import json, sys
argv = sys.argv[1:]
if argv == ["--version"]:
    print("jscpd {version}")
else:
    json.dump({{"duplicates": []}}, open(argv[argv.index("--output") + 1] + "/jscpd-report.json", "w"))
    open({record!r}, "w").write(json.dumps(argv))
"""


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "REPO_ROOT", tmp_path.resolve())
    return tmp_path.resolve()


def canned_report(root):
    """The jscpd 5.4.3 JSON of the real frontend scan, with names made absolute as ``--absolute`` does."""
    report = json.loads(CANNED.read_text())
    for clone in report["duplicates"]:
        for side in ("firstFile", "secondFile"):
            clone[side]["name"] = str(root / "frontend/src" / clone[side]["name"])
    return report


def canned_inputs(report):
    return {
        Path(clone[side]["name"]).relative_to(layout.REPO_ROOT).as_posix(): b"\n" * 700
        for clone in report["duplicates"]
        for side in ("firstFile", "secondFile")
    }


def fake_jscpd(tmp_path, version="5.4.3"):
    script = tmp_path / "fake-jscpd"
    script.write_text(FAKE_JSCPD.format(python=sys.executable, version=version, record=str(tmp_path / "argv.json")))
    script.chmod(0o755)
    return script


def test_ten_canned_clones_collapse_into_five_clusters(root):
    report = canned_report(root)
    findings = javascript._duplicate_diagnostics(report, canned_inputs(report))
    api, ui = "frontend/src/api/", "frontend/src/components/paper/"
    assert [(f.file_path, f.line) for f in findings] == [
        (api + "client.js", 54),
        (api + "dailyApi.js", 5),
        (ui + "DeepReviewSection.jsx", 80),
        (ui + "PaperDetailView/index.jsx", 95),
        ("frontend/src/offlineStore.js", 240),
    ]
    by_file = {f.file_path: f for f in findings}
    # client.js and libraryApi.js share one 12-line block
    assert by_file[api + "client.js"].message.endswith("also at " + api + "libraryApi.js:14-25")
    assert "2 places (largest clone 12 lines, 72 tokens)" in by_file[api + "client.js"].message
    # the error-parsing block: dailyApi overlapping ranges merge, reviewApi's adjacent ranges stay apart
    daily = by_file[api + "dailyApi.js"].message
    assert "5 places (largest clone 51 lines, 165 tokens)" in daily
    for location in ("goldenApi.js:21-53", "reviewApi.js:4-12", "reviewApi.js:13-31", "settingsApi.js:18-48"):
        assert api + location in daily
    # three self-clones in one file chain through the overlapping 289-295 / 291-296 ranges
    offline = by_file["frontend/src/offlineStore.js"].message
    assert "3 places" in offline and offline.endswith("offlineStore.js:289-296, frontend/src/offlineStore.js:455-462")
    assert {(f.rule, f.severity.value, f.category) for f in findings} == {("javascript/duplicate-code", "warning", "Duplication")}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["firstFile"].update(name="/elsewhere/x.js"),
        lambda c: c["firstFile"].update(start=0),
        lambda c: c["firstFile"].update(end=10**6),
        lambda c: c.update(tokens="72"),
    ],
)
def test_report_outside_the_explicit_batch_or_malformed_is_refused(root, mutate):
    report = canned_report(root)
    inputs = canned_inputs(report)
    mutate(report["duplicates"][0])
    with pytest.raises(ValueError):
        javascript._duplicate_diagnostics(report, inputs)


@pytest.mark.parametrize("mode", ["unset", "missing", "other-version"])
def test_adapter_is_skipped_with_one_note(root, monkeypatch, capsys, mode):
    if mode == "other-version":
        monkeypatch.setenv("DEADCODE_JSCPD", str(fake_jscpd(root, version="5.4.2")))
    elif mode == "missing":
        monkeypatch.setenv("DEADCODE_JSCPD", str(root / "absent"))
    else:
        monkeypatch.delenv("DEADCODE_JSCPD", raising=False)
    assert javascript._duplicates({"a.js": b"x\n", "b.js": b"x\n"}) == []
    notes = [line for line in capsys.readouterr().err.splitlines() if line.startswith("note:")]
    assert len(notes) == 1 and "javascript/duplicate-code skipped" in notes[0] and "5.4.3" in notes[0]


def test_only_non_test_js_and_jsx_reach_jscpd_with_absolute_paths_and_a_scratch_cwd(root, monkeypatch):
    monkeypatch.setenv("DEADCODE_JSCPD", str(fake_jscpd(root)))
    names = ["a.js", "b.jsx", "c.test.js", "d.test.jsx", "testSetup.js", "e.testkit.jsx", "__tests__/f.js", "tests/g.js", "h.ts", "i.mjs"]
    assert javascript._duplicates({name: b"x\n" for name in names}) == []
    argv = json.loads((root / "argv.json").read_text())
    assert [Path(a).relative_to(root).as_posix() for a in argv[argv.index("--silent") + 1 :]] == ["a.js", "b.jsx"]
    assert {"--min-tokens", "--min-lines", "--absolute"} <= set(argv) and "--config" not in argv


def test_run_appends_the_duplicate_findings_after_oxlint(root, monkeypatch):
    (root / "a.js").write_text("export const a = 1;\n")
    marker = javascript.Diagnostic("a.js", "code-quality", "javascript/duplicate-code", javascript.Severity.WARNING, "m", 1)
    monkeypatch.setattr(javascript, "_executable", lambda: "oxlint")
    monkeypatch.setattr(javascript, "_batch", lambda *_: [])
    monkeypatch.setattr(javascript, "_duplicates", lambda captured: [marker] if "a.js" in captured else [])
    assert javascript.run([Path("a.js")]) == [marker]


@pytest.fixture
def real_jscpd():
    executable = os.environ.get("DEADCODE_JSCPD")
    if not executable:
        pytest.skip("optional real jscpd integration requires DEADCODE_JSCPD")
    return executable


BLOCK = "".join(f"  const v{i} = compute(input, {i}) + other({i}, 'x');\n" for i in range(10))


def write_pair(root):
    (root / "one.js").write_text(f"export function one(input) {{\n{BLOCK}  return v1;\n}}\n")
    (root / "two.jsx").write_text(f"export function two(input) {{\n{BLOCK}  return v2;\n}}\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "three.js").write_text(f"export function three(input) {{\n{BLOCK}  return v3;\n}}\n")


def test_real_jscpd_reports_one_cluster_across_js_and_jsx_ignoring_target_config(root, real_jscpd):
    write_pair(root)
    (root / ".jscpd.json").write_text('{"minLines": 1000, "ignore": ["**/*.jsx"]}')
    (root / ".gitignore").write_text("ignored/\n")
    captured = {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.js*")) if p.name != ".jscpd.json"}
    findings = javascript._duplicates(captured)
    assert [(f.file_path, f.line, f.rule) for f in findings] == [("ignored/three.js", 1, "javascript/duplicate-code")]
    assert "3 places" in findings[0].message and "one.js:1-" in findings[0].message and "two.jsx:1-" in findings[0].message


def test_cli_scan_reports_the_cluster_end_to_end(root, real_jscpd):
    oxlint = os.environ.get("DEADCODE_TEST_OXLINT")
    if not oxlint:
        pytest.skip("optional real Oxlint integration requires DEADCODE_TEST_OXLINT")
    write_pair(root)
    environment = dict(os.environ, DEADCODE_REPO_ROOT=str(root), DEADCODE_OXLINT=oxlint, DEADCODE_JSCPD=real_jscpd)
    result = subprocess.run(
        [sys.executable, "-m", "deadcode_audit", "scan", "one.js", "two.jsx", "--json", "--no-history"],
        cwd=root, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0 and "note:" not in result.stderr, result.stderr
    rows = [row for row in json.loads(result.stdout)["diagnostics"] if row["rule"] == "javascript/duplicate-code"]
    assert [(row["filePath"], row["line"], row["severity"]) for row in rows] == [("one.js", 1, "warning")]
    assert "two.jsx:1-" in rows[0]["message"]


def test_real_jscpd_on_files_below_the_token_floor_is_clean_not_an_error(root, real_jscpd):
    (root / "tiny.js").write_text("module.exports = 1;\n")
    assert javascript._duplicates({"tiny.js": b"module.exports = 1;\n"}) == []
