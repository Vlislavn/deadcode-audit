"""Pinned local Oxlint adapter; quality findings, never proof of dead exports."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from deadcode_audit import diffscope
from deadcode_audit.diagnostic import (
    ENGINE_CODE_QUALITY,
    Diagnostic,
    RuleSpec,
    Severity,
)

SUFFIXES = frozenset({".js", ".cjs", ".mjs", ".jsx", ".ts", ".cts", ".mts", ".tsx"})
OXLINT_VERSION = "1.90.0"
BATCH_FILES = 128
SAFETY_DEFAULT_SECONDS = 60
SAFETY_MAX_SECONDS = 120
MAX_REPLY_BYTES = 8 * 1024 * 1024

_RULES = {
    "eslint(no-unused-vars)": (
        "javascript/unused-variable",
        Severity.WARNING,
        "Review local bindings; this does not prove that a host-invoked export is dead.",
    ),
    "eslint(no-unreachable)": (
        "javascript/unreachable",
        Severity.ERROR,
        "Review the unreachable statement; no source is automatically rewritten.",
    ),
    "eslint(no-empty)": (
        "javascript/empty-block",
        Severity.WARNING,
        "Review the empty block and its error-handling policy.",
    ),
    None: (
        "javascript/parse-error",
        Severity.ERROR,
        "Fix JavaScript/TypeScript syntax before relying on analysis.",
    ),
}
RULE_SPECS = tuple(
    RuleSpec(
        rule=rule,
        engine=ENGINE_CODE_QUALITY,
        default_severity=severity,
        category="JavaScript quality",
        help=help_text,
    )
    for rule, severity, help_text in _RULES.values()
)


def _run(executable: str, arguments: list[str]) -> subprocess.CompletedProcess:
    safety = int(
        os.environ.get("DEADCODE_JS_SAFETY_SECONDS", str(SAFETY_DEFAULT_SECONDS))
    )
    if not 1 <= safety <= SAFETY_MAX_SECONDS:
        raise ValueError(
            "DEADCODE_JS_SAFETY_SECONDS is outside the supported safety cap"
        )
    result = subprocess.run(
        [executable, *arguments],
        cwd=diffscope.REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=safety,
        check=False,
    )
    if (
        len(result.stdout.encode()) > MAX_REPLY_BYTES
        or len(result.stderr.encode()) > MAX_REPLY_BYTES
    ):
        raise ValueError("Oxlint reply exceeds the bounded batch size")
    if result.stderr.strip():
        raise ValueError("Oxlint returned unexpected stderr; analysis is not complete")
    return result


def _executable() -> str:
    requested = os.environ.get("DEADCODE_OXLINT", "oxlint")
    executable = shutil.which(requested)
    if executable is None:
        raise ValueError(
            f"Local Oxlint {OXLINT_VERSION} is required; set DEADCODE_OXLINT (no automatic download)"
        )
    result = _run(executable, ["--version"])
    if result.returncode != 0 or result.stdout.strip() != f"Version: {OXLINT_VERSION}":
        raise ValueError(
            f"Unsupported Oxlint protocol; exactly {OXLINT_VERSION} is required"
        )
    return executable


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key in Oxlint JSON")
        result[key] = value
    return result


def _location(row: dict, source: bytes) -> tuple[int, int]:
    labels = row.get("labels")
    if not isinstance(labels, list) or not labels or not isinstance(labels[0], dict):
        raise ValueError("Oxlint diagnostic has no source location")
    span = labels[0].get("span")
    if not isinstance(span, dict):
        raise ValueError("Oxlint diagnostic span is invalid")
    line, column, offset, length = (
        span.get(key) for key in ("line", "column", "offset", "length")
    )
    if any(type(value) is not int for value in (line, column, offset, length)):
        raise ValueError("Oxlint locations must be integers")
    lines = source.split(b"\n")
    if (
        line < 1
        or line > len(lines)
        or column < 1
        or column > len(lines[line - 1]) + 1
        or offset < 0
        or length < 0
        or offset + length > len(source)
    ):
        raise ValueError("Oxlint source location is outside the captured input")
    return line, column


def _diagnostic(row: dict, inputs: dict[str, bytes]) -> Diagnostic:
    if not isinstance(row, dict):
        raise ValueError("Oxlint diagnostic must be an object")
    filename = row.get("filename")
    if not isinstance(filename, str):
        raise ValueError("Oxlint diagnostic filename is missing")
    path = Path(filename)
    absolute = (
        path.resolve() if path.is_absolute() else (diffscope.REPO_ROOT / path).resolve()
    )
    if not absolute.is_relative_to(diffscope.REPO_ROOT):
        raise ValueError("Oxlint reported a path outside the target")
    relative = absolute.relative_to(diffscope.REPO_ROOT).as_posix()
    if relative not in inputs:
        raise ValueError("Oxlint reported a file outside the explicit batch")
    code = row.get("code")
    if not isinstance(code, (str, type(None))) or code not in _RULES:
        raise ValueError("Oxlint returned an unexpected rule")
    rule, severity, help_text = _RULES[code]
    if row.get("severity") != severity.value or not isinstance(row.get("message"), str):
        raise ValueError(
            "Oxlint diagnostic severity/message violates the pinned protocol"
        )
    line, column = _location(row, inputs[relative])
    return Diagnostic(
        file_path=relative,
        engine=ENGINE_CODE_QUALITY,
        rule=rule,
        severity=severity,
        message=row["message"],
        line=line,
        column=column,
        help=help_text,
        category="JavaScript quality",
        detail=f"Oxlint {OXLINT_VERSION}; advisory quality, not export reachability",
    )


def _batch(
    executable: str, config_path: Path, inputs: dict[str, bytes]
) -> list[Diagnostic]:
    arguments = [
        "--config",
        str(config_path),
        "--disable-nested-config",
        "--no-ignore",
        "--threads",
        "2",
        "-A",
        "all",
        "-A",
        "nursery",
        "-W",
        "no-unused-vars",
        "-D",
        "no-unreachable",
        "-W",
        "no-empty",
        "--format",
        "json",
        "--",
        *inputs,
    ]
    result = _run(executable, arguments)
    if result.returncode not in (0, 1):
        raise ValueError("Oxlint process failed instead of completing analysis")
    report = json.loads(result.stdout, object_pairs_hook=_unique)
    if not isinstance(report, dict) or type(report.get("number_of_files")) is not int:
        raise ValueError("Oxlint report has no integer coverage count")
    if report["number_of_files"] != len(inputs) or report.get("number_of_rules") != 3:
        raise ValueError("Oxlint coverage/rule count differs from the explicit batch")
    rows = report.get("diagnostics")
    if not isinstance(rows, list):
        raise ValueError("Oxlint diagnostics must be an array")
    findings = [_diagnostic(row, inputs) for row in rows]
    if (result.returncode == 1) != any(
        row.severity is Severity.ERROR for row in findings
    ):
        raise ValueError("Oxlint exit status contradicts its error diagnostics")
    return findings


def run(files: list[Path]) -> list[Diagnostic]:
    """Analyze explicit regular files; buffer findings until every batch is verified."""
    if not files:
        return []
    captured = {}
    for path in files:
        absolute = diffscope.REPO_ROOT / path
        if (
            path.suffix not in SUFFIXES
            or absolute.is_symlink()
            or not absolute.resolve().is_relative_to(diffscope.REPO_ROOT)
        ):
            raise ValueError(
                "JS input must be a supported regular file inside the target"
            )
        captured[path.as_posix()] = absolute.read_bytes()
    executable = _executable()
    findings = []
    paths = sorted(captured)
    with tempfile.TemporaryDirectory(prefix="deadcode-js-") as temporary:
        config_path = Path(temporary) / "oxlint.json"
        config_path.write_text('{"plugins":[]}', encoding="utf-8")
        for start in range(0, len(paths), BATCH_FILES):
            inputs = {
                path: captured[path] for path in paths[start : start + BATCH_FILES]
            }
            findings.extend(_batch(executable, config_path, inputs))
    if any(
        (diffscope.REPO_ROOT / path).read_bytes() != raw
        for path, raw in captured.items()
    ):
        raise ValueError("JS source changed during analysis; no findings are accepted")
    return findings
