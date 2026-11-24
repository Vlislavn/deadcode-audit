"""Size and churn between two committed snapshots, not a judgement of code quality.

Counts ported from agent-skills' change-checks (q521/D524). Physical lines include
comments and blanks; imports count instructions, not names. A short function has
at most five body lines after its initial docstring. Nested functions count once
as functions; physical file lines are never summed from function spans.
"""

from __future__ import annotations

import ast
import collections
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from deadcode_audit import diffscope, javascript, layout

METRICS = ("lines", "functions", "classes", "short_functions", "imports")
HOTSPOT_JS_SUFFIXES = javascript.JS_JSX_SUFFIXES
# Oxlint reports every function above complexity 1; correctness rules stay off so only complexity is listed.
_OXLINT_CONFIG = '{"plugins":[],"categories":{"correctness":"off"},"rules":{"complexity":["warn",{"max":1}]}}'
_OXLINT_MESSAGE = re.compile(r"[\w ]*?(?: `(.+?)`)? has a complexity of (\d+)\.")


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
    selected = [p for p in changed if p.endswith(".py") and not layout.is_test_path(Path(p)) and (paths is None or p in paths)]
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


def _function_lines(path: str, row: int) -> int | None:
    """Physical length of the function defined at ``row`` of a working-tree file; None if unreadable."""
    try:
        tree = ast.parse((layout.REPO_ROOT / path).read_text())
    except (SyntaxError, ValueError, OSError):
        return None
    return next((n.end_lineno - n.lineno + 1 for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno == row), None)


def _worst_functions(paths: list[str]) -> dict[str, tuple[str, int, int | None]]:
    """Per-file most complex function ``(name, McCabe, lines)`` from ruff's C901 (threshold 1 reports every branching function)."""
    root = layout.REPO_ROOT
    done = subprocess.run(
        ["ruff", "check", "--isolated", "--select", "C901", "--config", "lint.mccabe.max-complexity = 1", "--output-format", "json", *paths],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if done.returncode > 1:
        raise RuntimeError(f"ruff failed with exit code {done.returncode}: {done.stderr.strip()}")
    best: dict[str, tuple[int, int, str]] = {}  # path -> (complexity, -row, name); the earliest of equals wins
    for item in json.loads(done.stdout or "[]"):
        match = re.search(r"`(.+?)` is too complex \((\d+) > 1\)", item["message"])
        if match:
            key = Path(item["filename"]).resolve().relative_to(root.resolve()).as_posix()
            found = (int(match.group(2)), -item["location"]["row"], match.group(1))
            best[key] = max(best.get(key, found), found)
    return {path: (name, cc, _function_lines(path, -row)) for path, (cc, row, name) in best.items()}  # absent: no function above complexity 1


def _oxlint() -> str | None:
    """The pinned Oxlint executable that ``scan`` uses (DEADCODE_OXLINT or PATH); None when unusable."""
    try:
        return javascript._executable()
    except ValueError:
        return None


def _worst_js_functions(paths: list[str], executable: str) -> dict[str, tuple[str, int, int | None]]:
    """Per-file most complex function ``(name, complexity, lines)`` from Oxlint's ``complexity`` rule at max 1."""
    root = layout.REPO_ROOT.resolve()
    best: dict[str, tuple[int, int, int, str, int]] = {}  # path -> (complexity, -line, -column, name, lines); the earliest of equals wins
    sources: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="deadcode-growth-") as temporary:
        config = Path(temporary) / "oxlint.json"
        config.write_text(_OXLINT_CONFIG, encoding="utf-8")
        for start in range(0, len(paths), javascript.BATCH_FILES):
            args = ["--config", str(config), "--disable-nested-config", "--no-ignore", "--threads", "2", "--format", "json", "--", *paths[start:start + javascript.BATCH_FILES]]
            done = javascript._run(executable, args)
            if done.returncode > 1:
                raise RuntimeError(f"oxlint failed with exit code {done.returncode}")
            for item in json.loads(done.stdout)["diagnostics"]:
                match = item["code"] == "eslint(complexity)" and _OXLINT_MESSAGE.match(item["message"])
                if not match:
                    continue  # e.g. a parse error: that file keeps no complexity
                key = (root / item["filename"]).resolve().relative_to(root).as_posix()
                span = item["labels"][0]["span"]
                source = sources.setdefault(key, (root / key).read_bytes())
                length = source.count(b"\n", span["offset"], span["offset"] + span["length"]) + 1
                name = match.group(1) or f"anonymous@{span['line']}:{span['column']}"
                found = (int(match.group(2)), -span["line"], -span["column"], name, length)
                best[key] = max(best.get(key, found), found, key=lambda entry: entry[:3])
    return {path: (name, cc, length) for path, (cc, _, _, name, length) in best.items()}


def _is_hotspot_file(path: str) -> bool:
    """Non-test .py/.js/.jsx; JS tests are ``javascript.is_js_test_file``."""
    file = Path(path)
    if layout.is_test_path(file):
        return False
    return file.suffix == ".py" or (file.suffix in HOTSPOT_JS_SUFFIXES and not javascript.is_js_test_file(file))


def hotspots(base: str | None, head: str = "HEAD", paths: list[str] | None = None, top: int = 15) -> dict:
    """Rank non-test Python and JS/JSX files at head by commits touching them x lines x max complexity.

    Without ``base`` the whole history of ``head`` counts. Commit counts come from
    ``git log --name-only``; complexity comes from ruff (Python) or the pinned Oxlint
    ``complexity`` rule (JS/JSX) on the working tree, which also names each file's most
    complex function (``worst_function``, ``worst_lines``). A language's rows omit both
    (score = commits x lines) when its tool is unavailable: ``ruff`` / ``oxlint`` false
    in the report (``oxlint`` is None when no JS file is scored).
    """
    head = diffscope.git_output("rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}").strip()
    span = head if base is None else f"{diffscope.git_output('rev-parse', '--verify', '--end-of-options', f'{base}^{{commit}}').strip()}..{head}"
    log = diffscope.git_output("--literal-pathspecs", "log", "--name-only", "--format=", "-z", "--no-renames", span, "--")
    churn = collections.Counter(filter(None, log.split("\0")))
    present = set(diffscope.git_output("ls-tree", "-r", "--name-only", "-z", head).split("\0"))
    selected = sorted(p for p in churn if p in present and _is_hotspot_file(p) and (paths is None or p in paths))
    python = [p for p in selected if p.endswith(".py")]
    js = [p for p in selected if p not in python]
    has_ruff = shutil.which("ruff") is not None
    oxlint = _oxlint() if js else None
    worst = _worst_functions(python) if has_ruff and python else {}
    worst.update(_worst_js_functions(js, oxlint) if oxlint else {})
    rows = []
    for path in selected:
        lines = len(diffscope.git_output("show", f"{head}:{path}").splitlines())
        row = {"path": path, "commits": churn[path], "lines": lines}
        name, cc, length = worst.get(path, (None, 1, None))  # no function above complexity 1
        if has_ruff if path.endswith(".py") else oxlint is not None:
            row.update(max_cc=cc, worst_function=name, worst_lines=length)
        row["score"] = churn[path] * lines * row.get("max_cc", 1)
        rows.append(row)
    rows.sort(key=lambda row: (-row["score"], row["path"]))
    return {"base": base, "head": head, "ruff": has_ruff, "oxlint": None if not js else oxlint is not None, "scored_files": len(rows), "files": rows[:top] if top > 0 else rows}


def _worst_label(row: dict) -> str:
    if row.get("worst_function") is None:
        return ""
    return f"  ({row['worst_function']}, {'?' if row['worst_lines'] is None else row['worst_lines']} lines)"


def render_hotspots(report: dict) -> str:
    """Ranked table; facts for choosing where to look, not a verdict."""
    cc = any("max_cc" in r for r in report["files"])
    lines = [f"Hotspots (commits x lines{' x max complexity' if cc else ''}), top {len(report['files'])} of {report['scored_files']} non-test Python/JS files:"]
    lines.append(f"{'score':>9} {'commits':>7} {'lines':>6}{' max_cc' if cc else ''}  path{'  (worst function)' if cc else ''}")
    lines.extend(f"{r['score']:>9} {r['commits']:>7} {r['lines']:>6}{format(r['max_cc'], '>7') if 'max_cc' in r else ' ' * 7 if cc else ''}  {r['path']}{_worst_label(r)}" for r in report["files"])
    if not report["ruff"]:
        lines.append("ruff not on PATH: Python complexity column omitted; score = commits x lines.")
    if report["oxlint"] is False:
        lines.append(f"Oxlint {javascript.OXLINT_VERSION} not available (set DEADCODE_OXLINT): JS complexity omitted; score = commits x lines.")
    return "\n".join(lines)


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
