"""CLI contracts for the optional local JS/TS quality backend, not dead-export proof."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CLI_SAFETY_DEFAULT_SECONDS = 60
CLI_SAFETY_MAX_SECONDS = 120


def run_cli(root, *arguments, backend=None):
    environment = dict(os.environ, DEADCODE_REPO_ROOT=str(root))
    if backend is not None:
        environment["DEADCODE_OXLINT"] = str(backend)
    safety = int(
        os.environ.get("DEADCODE_TEST_SAFETY_SECONDS", str(CLI_SAFETY_DEFAULT_SECONDS))
    )
    assert 1 <= safety <= CLI_SAFETY_MAX_SECONDS
    return subprocess.run(
        [sys.executable, "-m", "deadcode_audit", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=safety,
    )


@pytest.fixture
def real_backend():
    executable = os.environ.get("DEADCODE_TEST_OXLINT")
    if not executable:
        pytest.skip("optional real Oxlint integration requires DEADCODE_TEST_OXLINT")
    assert Path(executable).is_file()
    return executable


def test_js_only_cli_uses_real_backend_and_does_not_execute_source(
    tmp_path, real_backend
):
    marker = tmp_path / "must-not-exist"
    source = tmp_path / "entry.cjs"
    source.write_text(
        f"require('node:fs').writeFileSync({json.dumps(str(marker))}, 'executed');\n"
        "const unusedLocal = 1;\nmodule.exports = (ctx) => ctx.app;\n"
    )
    result = run_cli(
        tmp_path, "scan", "entry.cjs", "--json", "--no-history", backend=real_backend
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["files_scanned"] == 1
    assert report["coverage"] == {"python": 0, "javascript_typescript": 1}
    assert {row["rule"] for row in report["diagnostics"]} == {
        "javascript/unused-variable"
    }
    assert not marker.exists()


def test_mixed_python_and_js_share_report_and_severity_policy(tmp_path, real_backend):
    (tmp_path / "src").mkdir()
    (tmp_path / ".deadcode.yml").write_text(
        "project:\n  source_roots: [src]\nrules:\n  javascript/unused-variable: off\n"
    )
    (tmp_path / "src/good.py").write_text("answer = 42\n")
    (tmp_path / "src/entry.js").write_text(
        "const unusedLocal = 1;\nmodule.exports = (ctx) => ctx.app;\n"
    )
    result = run_cli(tmp_path, "scan", "--json", "--no-history", backend=real_backend)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["files_scanned"] == 2
    assert report["coverage"] == {"python": 1, "javascript_typescript": 1}
    assert report["diagnostics"] == []


def test_javascript_syntax_errors_are_visible_and_block_ci(tmp_path, real_backend):
    (tmp_path / "broken.js").write_text("const broken = ;\n")
    result = run_cli(tmp_path, "ci", "broken.js", "--json", backend=real_backend)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["files_scanned"] == 1
    assert report["summary"]["errors"] > 0
    assert {row["rule"] for row in report["diagnostics"]} == {"javascript/parse-error"}


def test_missing_js_backend_fails_instead_of_an_empty_success(tmp_path):
    (tmp_path / "entry.js").write_text("module.exports = 1;\n")
    result = run_cli(
        tmp_path,
        "scan",
        "entry.js",
        "--json",
        "--no-history",
        backend=tmp_path / "absent-backend",
    )
    assert result.returncode != 0
    assert not result.stdout.strip()


@pytest.mark.parametrize(
    "suffix", ["js", "cjs", "mjs", "jsx", "ts", "cts", "mts", "tsx"]
)
def test_supported_syntax_extensions_and_host_exports(tmp_path, real_backend, suffix):
    body = "module.exports = (ctx) => { window.Notice; return ctx.app; };\n"
    if suffix in {"ts", "cts", "mts", "tsx"}:
        body = "export const invoke = (ctx: { app: number }) => ctx.app;\n"
    (tmp_path / ("entry." + suffix)).write_text(body)
    result = run_cli(
        tmp_path,
        "scan",
        "entry." + suffix,
        "--json",
        "--no-history",
        backend=real_backend,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["files_scanned"] == 1 and report["diagnostics"] == []


def test_source_exclusions_and_overlapping_explicit_paths(tmp_path, real_backend):
    source = tmp_path / "src"
    source.mkdir()
    (source / "good.js").write_text("module.exports = 1;\n")
    (source / "ignored.js").write_text("const = ;\n")
    (tmp_path / ".deadcode.yml").write_text(
        "project:\n  source_roots: [src]\nexclude: [src/ignored.js]\n"
    )
    result = run_cli(
        tmp_path,
        "scan",
        "src",
        "src/good.js",
        "--json",
        "--no-history",
        backend=real_backend,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["files_scanned"] == 1 and report["diagnostics"] == []


def test_backend_ignores_project_js_config_and_never_follows_its_plugins(
    tmp_path, real_backend
):
    marker = tmp_path / "config-executed"
    (tmp_path / "oxlint.config.js").write_text(
        f"require('fs').writeFileSync({json.dumps(str(marker))}, 'bad'); throw new Error('must not load');\n"
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".oxlintrc.json").write_text('{"extends": ["missing-config.json"]}')
    (nested / "entry.cjs").write_text("module.exports = (ctx) => ctx.app;\n")
    result = run_cli(
        tmp_path, "scan", "nested", "--json", "--no-history", backend=real_backend
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_changed_scope_keeps_python_only_api_and_scans_new_committed_js(
    tmp_path, real_backend, monkeypatch
):
    from deadcode_audit import diffscope

    def git(*arguments):
        subprocess.run(
            ["git", *arguments], cwd=tmp_path, check=True, capture_output=True
        )

    git("init", "-b", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/base.py").write_text("answer = 42\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "base",
    )
    git("checkout", "-b", "fixture-change")
    (tmp_path / "src/added.py").write_text("answer = 43\n")
    (tmp_path / "src/added.js").write_text("module.exports = 44;\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "change",
    )
    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    assert diffscope.changed_python_files("main") == [Path("src/added.py")]
    result = run_cli(
        tmp_path,
        "scan",
        "--changed",
        "--compare-branch",
        "main",
        "--json",
        "--no-history",
        backend=real_backend,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["coverage"] == {
        "python": 1,
        "javascript_typescript": 1,
    }


def test_python_only_scan_result_and_json_remain_unchanged(tmp_path, monkeypatch):
    from deadcode_audit import config, diffscope, output, scan
    from deadcode_audit.detectors import ALL_DETECTORS

    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    (tmp_path / "entry.py").write_text("answer = 42\n")
    cfg = config.DeadcodeConfig()
    before = scan.run_scan([Path("entry.py")], ALL_DETECTORS, cfg)
    after = scan.run_source_scan([Path("entry.py")], ALL_DETECTORS, cfg)
    assert before == after
    assert "coverage" not in json.loads(
        output.render_json(
            after.diagnostics,
            after.score,
            files_scanned=after.file_count,
            coverage=after.coverage,
        )
    )


def backend_report(*, rows=None, count=1):
    return {"diagnostics": rows or [], "number_of_files": count, "number_of_rules": 3}


def backend_row():
    return {
        "filename": "entry.js",
        "code": "eslint(no-unused-vars)",
        "severity": "warning",
        "message": "fixture",
        "labels": [{"span": {"offset": 6, "length": 1, "line": 1, "column": 7}}],
    }


@pytest.mark.parametrize(
    "fault",
    [
        "json",
        "duplicate-key",
        "count",
        "count-bool",
        "rules",
        "rows",
        "outside",
        "undeclared",
        "line",
        "column",
        "offset",
        "rule",
        "severity",
        "labels",
        "row",
        "rc",
        "contradiction",
    ],
)
def test_invalid_backend_protocol_is_never_clean(tmp_path, monkeypatch, fault):
    from deadcode_audit import diffscope, javascript

    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    row = backend_row()
    report = backend_report(rows=[row])
    code = 0
    if fault == "count":
        report["number_of_files"] = 0
    if fault == "count-bool":
        report["number_of_files"] = True
    if fault == "rules":
        report["number_of_rules"] = 0
    if fault == "rows":
        report["diagnostics"] = {}
    if fault == "outside":
        row["filename"] = "../outside.js"
    if fault == "undeclared":
        row["filename"] = "other.js"
    if fault == "line":
        row["labels"][0]["span"]["line"] = 999
    if fault == "column":
        row["labels"][0]["span"]["column"] = 999
    if fault == "offset":
        row["labels"][0]["span"]["offset"] = 999
    if fault == "rule":
        row["code"] = "unknown/rule"
    if fault == "severity":
        row["severity"] = "error"
    if fault == "labels":
        row["labels"] = []
    if fault == "row":
        report["diagnostics"] = [None]
    if fault == "rc":
        code = 2
    if fault == "contradiction":
        code = 1
    raw = json.dumps(report)
    if fault == "json":
        raw = "not-json"
    if fault == "duplicate-key":
        raw = '{"diagnostics": [], "diagnostics": []}'
    monkeypatch.setattr(
        javascript, "_run", lambda *_: subprocess.CompletedProcess([], code, raw, "")
    )
    with pytest.raises((ValueError, TypeError)):
        javascript._batch(
            "fixture", tmp_path / "config.json", {"entry.js": b"const x = 1;\n"}
        )


@pytest.mark.parametrize("fault", ["stderr", "timeout", "oversized", "invalid-budget"])
def test_backend_transport_failures_do_not_return_findings(
    tmp_path, monkeypatch, fault
):
    from deadcode_audit import diffscope, javascript

    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)

    def process(*args, **kwargs):
        if fault == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(
            [],
            0,
            "x" * (javascript.MAX_REPLY_BYTES + 1) if fault == "oversized" else "{}",
            "failure" if fault == "stderr" else "",
        )

    monkeypatch.setattr(javascript.subprocess, "run", process)
    if fault == "invalid-budget":
        monkeypatch.setenv("DEADCODE_JS_SAFETY_SECONDS", "99999")
    with pytest.raises((ValueError, subprocess.TimeoutExpired)):
        javascript._run("fixture", [])


def test_unsupported_backend_version_is_refused(tmp_path, monkeypatch):
    from deadcode_audit import javascript

    monkeypatch.setattr(javascript.shutil, "which", lambda _: "fixture")
    monkeypatch.setattr(
        javascript,
        "_run",
        lambda *_: subprocess.CompletedProcess([], 0, "Version: 9.9.9\n", ""),
    )
    with pytest.raises(ValueError):
        javascript._executable()


def test_source_drift_and_failed_later_batch_do_not_publish_a_partial_scan(
    tmp_path, monkeypatch
):
    from deadcode_audit import diffscope, javascript

    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(javascript, "BATCH_FILES", 1)
    monkeypatch.setattr(javascript, "_executable", lambda: "fixture")
    for name in ["a.js", "b.js"]:
        (tmp_path / name).write_text("module.exports = 1;\n")

    def mutate(*_):
        (tmp_path / "a.js").write_text("module.exports = 2;\n")
        return []

    monkeypatch.setattr(javascript, "_batch", mutate)
    with pytest.raises(ValueError):
        javascript.run([Path("a.js"), Path("b.js")])
    calls = []

    def partial(*_):
        calls.append(True)
        if len(calls) == 2:
            raise ValueError("second batch failed")
        return []

    monkeypatch.setattr(javascript, "_batch", partial)
    with pytest.raises(ValueError):
        javascript.run([Path("a.js"), Path("b.js")])
