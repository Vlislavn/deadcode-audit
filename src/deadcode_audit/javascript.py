"""Pinned local Oxlint adapter (+ optional pinned jscpd); quality findings, never proof of dead exports."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from deadcode_audit import layout
from deadcode_audit.diagnostic import (
    ENGINE_CODE_QUALITY,
    Diagnostic,
    RuleSpec,
    Severity,
)

SUFFIXES = frozenset({".js", ".cjs", ".mjs", ".jsx", ".ts", ".cts", ".mts", ".tsx"})
OXLINT_VERSION = "1.90.0"
JSCPD_VERSION = "5.4.3"
JSCPD_EMPTY_WARNING = "Warning: jscpd analyzed no files"  # every input below the token floor: not a failure
JS_JSX_SUFFIXES = (".js", ".jsx")
# JS test/helper files: not production code, so duplication and hotspot ranking skip them.
TEST_FILE_SUFFIXES = (".test.js", ".test.jsx", ".testkit.js", ".testkit.jsx", "testSetup.js")
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
_OXLINT_SPECS = tuple(
    RuleSpec(
        rule=rule,
        engine=ENGINE_CODE_QUALITY,
        default_severity=severity,
        category="JavaScript quality",
        help=help_text,
    )
    for rule, severity, help_text in _RULES.values()
)
DUPLICATE_RULE = RuleSpec(
    rule="javascript/duplicate-code",
    engine=ENGINE_CODE_QUALITY,
    default_severity=Severity.WARNING,
    category="Duplication",
    help="The same block is copied in several places; extract it once and reuse it, or confirm the copies must diverge.",
)
RULE_SPECS = (*_OXLINT_SPECS, DUPLICATE_RULE)


def is_js_test_file(path: Path) -> bool:
    """``*.test.js(x)``, ``*.testkit.js(x)``, ``testSetup.js`` and anything under ``__tests__/`` (Python tests: ``layout.is_test_path``)."""
    return "__tests__" in path.parts or path.name.endswith(TEST_FILE_SUFFIXES)


def _run(executable: str, arguments: list[str], tool: str = "Oxlint", cwd: Path | None = None, tolerated: str = "") -> subprocess.CompletedProcess:
    safety = int(
        os.environ.get("DEADCODE_JS_SAFETY_SECONDS", str(SAFETY_DEFAULT_SECONDS))
    )
    if not 1 <= safety <= SAFETY_MAX_SECONDS:
        raise ValueError(
            "DEADCODE_JS_SAFETY_SECONDS is outside the supported safety cap"
        )
    result = subprocess.run(
        [executable, *arguments],
        cwd=cwd or layout.REPO_ROOT,
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
        raise ValueError(f"{tool} reply exceeds the bounded batch size")
    if any(line.strip() and not (tolerated and line.startswith(tolerated)) for line in result.stderr.splitlines()):
        raise ValueError(f"{tool} returned unexpected stderr; analysis is not complete")
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
        path.resolve() if path.is_absolute() else (layout.REPO_ROOT / path).resolve()
    )
    if not absolute.is_relative_to(layout.REPO_ROOT):
        raise ValueError("Oxlint reported a path outside the target")
    relative = absolute.relative_to(layout.REPO_ROOT).as_posix()
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
        absolute = layout.REPO_ROOT / path
        if (
            path.suffix not in SUFFIXES
            or absolute.is_symlink()
            or not absolute.resolve().is_relative_to(layout.REPO_ROOT)
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
    findings.extend(_duplicates(captured))
    if any(
        (layout.REPO_ROOT / path).read_bytes() != raw
        for path, raw in captured.items()
    ):
        raise ValueError("JS source changed during analysis; no findings are accepted")
    return findings


def _jscpd_executable() -> str | None:
    """The pinned jscpd, or ``None`` after ONE stderr note: the adapter is optional."""
    requested = os.environ.get("DEADCODE_JSCPD")
    try:
        executable = shutil.which(requested) if requested else None
        if executable is None:
            raise ValueError("DEADCODE_JSCPD does not name an executable")
        result = _run(executable, ["--version"], "jscpd")
        if result.returncode != 0 or result.stdout.strip() != f"jscpd {JSCPD_VERSION}":
            raise ValueError(f"exactly jscpd {JSCPD_VERSION} is required")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"note: javascript/duplicate-code skipped ({error}); set DEADCODE_JSCPD to jscpd {JSCPD_VERSION}", file=sys.stderr)
        return None
    return executable


def _range(side: object, inputs: dict[str, bytes]) -> tuple[str, int, int]:
    """One clone side as ``(repo-relative path, first line, last line)``; jscpd ran with ``--absolute``."""
    name = side.get("name") if isinstance(side, dict) else None
    start, end = (side.get(key) for key in ("start", "end")) if isinstance(side, dict) else (None, None)
    if not isinstance(name, str) or type(start) is not int or type(end) is not int:
        raise ValueError("jscpd clone location is malformed")
    path = Path(name).resolve()
    relative = path.relative_to(layout.REPO_ROOT).as_posix() if path.is_relative_to(layout.REPO_ROOT) else None
    if relative not in inputs:
        raise ValueError("jscpd reported a file outside the explicit batch")
    if not 1 <= start <= end <= inputs[relative].count(b"\n") + 1:
        raise ValueError("jscpd location is outside the captured input")
    return relative, start, end


def _clusters(report: object, inputs: dict[str, bytes]) -> list[tuple[list[tuple[str, int, int]], int, int]]:
    """Clones joined transitively when they share a file range: ``(locations, lines, tokens)`` per cluster."""
    rows = report.get("duplicates") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        raise ValueError("jscpd duplicates must be an array")
    clones = []
    for row in rows:
        lines, tokens = (row.get(key) if isinstance(row, dict) else None for key in ("lines", "tokens"))
        if type(lines) is not int or type(tokens) is not int:
            raise ValueError("jscpd clone size is malformed")
        clones.append((_range(row.get("firstFile"), inputs), _range(row.get("secondFile"), inputs), lines, tokens))
    parent: dict[tuple[str, int, int], tuple[str, int, int]] = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = node = parent[parent[node]]
        return node

    for first, second, _, _ in clones:
        parent[find(first)] = find(second)
    ordered = sorted(parent)
    for before, after in zip(ordered, ordered[1:]):  # overlapping ranges of one file are one location
        if before[0] == after[0] and after[1] <= before[2]:
            parent[find(after)] = find(before)
    groups: dict[tuple[str, int, int], list] = {}
    for first, _, lines, tokens in clones:
        group = groups.setdefault(find(first), [set(), 0, 0])
        group[1], group[2] = max((group[1], group[2]), (lines, tokens))
    for node in parent:
        groups[find(node)][0].add(node)
    return sorted(([_merge(group[0]), group[1], group[2]] for group in groups.values()), key=lambda c: c[0][0])


def _merge(ranges: set) -> list[tuple[str, int, int]]:
    merged: list[tuple[str, int, int]] = []
    for path, start, end in sorted(ranges):
        if merged and merged[-1][0] == path and start <= merged[-1][2]:
            merged[-1] = (path, merged[-1][1], max(end, merged[-1][2]))
        else:
            merged.append((path, start, end))
    return merged


def _duplicate_diagnostics(report: object, inputs: dict[str, bytes]) -> list[Diagnostic]:
    findings = []
    for (first, *others), lines, tokens in _clusters(report, inputs):
        also = ", ".join(f"{path}:{start}-{end}" for path, start, end in others)
        findings.append(Diagnostic(
            file_path=first[0],
            engine=DUPLICATE_RULE.engine,
            rule=DUPLICATE_RULE.rule,
            severity=DUPLICATE_RULE.default_severity,
            message=f"Code block duplicated in {len(others) + 1} places (largest clone {lines} lines, {tokens} tokens); also at {also}",
            line=first[1],
            help=DUPLICATE_RULE.help,
            category=DUPLICATE_RULE.category,
            detail=f"jscpd {JSCPD_VERSION}; exact token clones (min 50 tokens, 5 lines); advisory, not proof of a defect",
        ))
    return findings


def _duplicates(captured: dict[str, bytes]) -> list[Diagnostic]:
    """Optional jscpd pass over non-test JS/JSX: one finding per transitive clone cluster."""
    inputs = {path: raw for path, raw in captured.items()
              if path.endswith(JS_JSX_SUFFIXES) and not is_js_test_file(Path(path)) and not layout.is_test_path(Path(path))}
    executable = _jscpd_executable() if inputs else None
    if executable is None:
        return []
    with tempfile.TemporaryDirectory(prefix="deadcode-jscpd-") as temporary:
        # Absolute inputs (jscpd's own names are relative to an unspecified common root) and a scratch cwd:
        # a target .jscpd.json is never read, so it cannot change the verdict.
        result = _run(executable, [
            "--cross-formats", "javascript,jsx", "--min-tokens", "50", "--min-lines", "5", "--absolute", "--no-gitignore",
            "--reporters", "json", "--output", temporary, "--silent",
            *(str(layout.REPO_ROOT / path) for path in sorted(inputs)),
        ], "jscpd", Path(temporary), JSCPD_EMPTY_WARNING)
        if result.returncode != 0:
            raise ValueError("jscpd process failed instead of completing analysis")
        report = json.loads((Path(temporary) / "jscpd-report.json").read_text(encoding="utf-8"), object_pairs_hook=_unique)
    return _duplicate_diagnostics(report, inputs)
