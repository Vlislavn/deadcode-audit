"""Size and churn between two committed snapshots, not a judgement of code quality.

Counts ported from agent-skills' change-checks (q521/D524). Physical lines include
comments and blanks; imports count instructions, not names. A short function has
at most five body lines after its initial docstring. Nested functions count once
as functions; physical file lines are never summed from function spans.
"""

from __future__ import annotations

import ast
from pathlib import Path

from deadcode_audit import diffscope

METRICS = ("lines", "functions", "classes", "short_functions", "imports")


def _counts(text: str) -> dict[str, int | None]:
    counts = dict.fromkeys(METRICS, 0)
    counts["lines"] = len(text.splitlines())
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {key: counts[key] if key == "lines" else None for key in METRICS}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body = body[1:]
            counts["functions"] += 1
            counts["short_functions"] += (body[-1].end_lineno - body[0].lineno + 1 if body else 0) <= 5
        elif isinstance(node, ast.ClassDef):
            counts["classes"] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            counts["imports"] += 1
    return counts


def _snapshot(rev: str, paths: list[str]) -> tuple[dict, list[dict]]:
    # Enumerating the snapshot distinguishes an absent blob (add/delete) from a
    # failed git read. Never replace a read error with a zero-sized file.
    present = set(diffscope.git_output("ls-tree", "-r", "--name-only", "-z", rev).split("\0"))
    totals = dict.fromkeys(METRICS, 0)
    files = []
    for path in paths:
        if path not in present:
            files.append({"path": path, "status": "absent"})
            continue
        text = diffscope.git_output("show", f"{rev}:{path}")
        values = _counts(text)
        files.append({"path": path, "status": "unknown" if values["functions"] is None else "counted"})
        for key in METRICS:
            totals[key] = None if totals[key] is None or values[key] is None else totals[key] + values[key]
    return totals, files


def _churn(base: str, head: str, paths: list[str] | None = None) -> dict[str, int]:
    totals = {"added": 0, "deleted": 0, "binary_files": 0}
    if paths == []:
        return totals
    raw = diffscope.git_output("--literal-pathspecs", "diff", "--no-ext-diff", "--numstat", "-z", "--no-renames", base, head, "--", *(paths or []))
    for entry in filter(None, raw.split("\0")):
        added, deleted, _ = entry.split("\t", 2)
        if added == "-":
            totals["binary_files"] += 1
        else:
            totals["added"] += int(added)
            totals["deleted"] += int(deleted)
    return totals


def compare(base: str, head: str = "HEAD", paths: list[str] | None = None) -> dict:
    """Measure changed non-test Python files; optional paths narrow that scope.

    Revisions are exact endpoints, not a merge base. No checkout, model, history
    write, threshold, or execution of the target project is involved.
    """
    base = diffscope.git_output("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    head = diffscope.git_output("rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}").strip()
    changed = list(filter(None, diffscope.git_output("diff", "--no-ext-diff", "--name-only", "-z", "--no-renames", base, head).split("\0")))
    selected = [p for p in changed if p.endswith(".py") and not diffscope.is_test_path(Path(p)) and (paths is None or p in paths)]
    before, base_files = _snapshot(base, selected)
    after, head_files = _snapshot(head, selected)
    return {
        "base": base, "head": head, "scope": "changed non-test Python files",
        "files": selected, "excluded": [p for p in changed if p not in selected],
        "before": before, "after": after,
        "delta": {key: None if before[key] is None or after[key] is None else after[key] - before[key] for key in METRICS},
        "coverage": {"base": base_files, "head": head_files},
        "churn": {"python": _churn(base, head, selected), "all": _churn(base, head)},
    }


def render(report: dict) -> str:
    """Human summary of the same facts as JSON; unknown AST counts stay unknown."""
    values = "; ".join(
        f"{key}: unknown" if report["delta"][key] is None else
        f"{key} {report['before'][key]} -> {report['after'][key]} ({report['delta'][key]:+d})"
        for key in METRICS
    )
    lines = [f"Size ({report['scope']}, {len(report['files'])}): {values}."]
    for side, entries in report["coverage"].items():
        lines.extend(f"AST unknown: {entry['path']}@{report[side][:8]}" for entry in entries if entry["status"] == "unknown")
    lines.append("Churn: " + "; ".join(f"{scope} +{n['added']}/-{n['deleted']} (binary files: {n['binary_files']})" for scope, n in report["churn"].items()))
    lines.append("Facts, not a bloat verdict; growth does not change the exit code.")
    return "\n".join(lines)
