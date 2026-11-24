"""Advisory stub-sensitivity audit: which function bodies can be replaced without any test noticing.

Every top-level function/method in the selected files is stubbed in a private scratch copy of the
repository (``return None``; ``raise RuntimeError("stubbed")`` when the function already only returns
None) and only the tests that executed it in a green coverage baseline are re-run. A function whose
stub no test detects is *pseudo-tested or dead* (paper arXiv:2611.21797 §3, vacuous-test rate).
The target working tree is never written.
"""

from __future__ import annotations

import ast
import codecs
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from deadcode_audit import diffscope, mutation

_PLUGIN = '''
"""Pytest plugin: record, per test node id, the lines it executed (coverage dynamic contexts)."""
import os

import coverage

_cov = None


def pytest_configure(config):
    global _cov
    _cov = coverage.Coverage(config_file=False, data_file=os.environ["STUB_COV_DATA"], source=[os.environ["STUB_COV_SOURCE"]])
    _cov.start()


def pytest_runtest_protocol(item, nextitem):
    _cov.switch_context(item.nodeid)


def pytest_unconfigure(config):
    _cov.stop()
    _cov.save()
'''


_MAX_ARGV_CHARS = 100_000  # well under the OS argument limit (macOS 1 MiB incl. environment)


_COLLECTION_ERROR_MARKERS = ("ERROR collecting", "errors during collection", "found no collectors", "while loading conftest",
                              "while parsing the following warning configuration")


class HarnessError(RuntimeError):
    """The audit itself failed (red baseline, process failure, parse error); never a clean result."""


@dataclass
class FunctionSpan:
    locator: str
    first_line: int  # first line replaced by the stub (1-based)
    last_line: int
    indent: str
    stub_kind: str


@dataclass
class StubResult:
    file: str
    function: str
    stub_kind: str
    detected: bool
    detected_by: str | None
    tests_run: int
    seconds: float
    reason: str | None = None


@dataclass
class Report:
    results: list[StubResult] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def _own_nodes(body: list[ast.stmt]):
    """Yield nodes of a function body without descending into nested scopes."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            stack.extend(ast.iter_child_nodes(node))


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        names.add(target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", ""))
    return names


def _skip_reason(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    if _decorator_names(node) & {"abstractmethod", "overload"}:
        return "abstract or overload"
    if any(isinstance(n, ast.Yield | ast.YieldFrom) for n in _own_nodes(node.body)):
        return "generator"
    statements = node.body[1:] if _is_docstring(node.body[0]) else node.body
    if all(isinstance(s, ast.Pass) or _is_docstring(s) for s in statements):
        return "empty body"
    if node.body[0].lineno == node.lineno:
        return "inline body"
    return None


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and (
        isinstance(stmt.value.value, str) or stmt.value.value is Ellipsis
    )


def _stub_kind(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    returns_value = any(
        isinstance(n, ast.Return) and not (n.value is None or (isinstance(n.value, ast.Constant) and n.value.value is None))
        for n in _own_nodes(node.body)
    )
    return "return_none" if returns_value else "raise_runtime_error"


def find_functions(source: str) -> tuple[list[FunctionSpan], list[dict[str, str]]]:
    """Top-level functions and methods of top-level classes, plus skipped ones with a reason."""
    tree = ast.parse(source)
    found: list[FunctionSpan] = []
    skipped: list[dict[str, str]] = []
    candidates: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            candidates.append((stmt.name, stmt))
        elif isinstance(stmt, ast.ClassDef):
            candidates.extend(
                (f"{stmt.name}.{m.name}", m) for m in stmt.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
            )
    for locator, node in candidates:
        reason = _skip_reason(node)
        if reason:
            skipped.append({"function": locator, "reason": reason})
            continue
        first = node.body[1] if _is_docstring(node.body[0]) and len(node.body) > 1 else node.body[0]
        # a decorated nested def/class starts at its first decorator, not at the `def` line
        decorators = getattr(first, "decorator_list", [])
        first_line = min([first.lineno, *(d.lineno for d in decorators)])
        found.append(
            FunctionSpan(locator, first_line, node.end_lineno or first.lineno, " " * first.col_offset, _stub_kind(node))
        )
    return found, skipped


def stub_source(source: str, span: FunctionSpan) -> str:
    """Return ``source`` with the lines of ``span`` replaced by the stub; the result must parse."""
    stmt = "return None" if span.stub_kind == "return_none" else 'raise RuntimeError("stubbed")'
    lines = source.splitlines(keepends=True)
    stubbed = "".join(lines[: span.first_line - 1]) + f"{span.indent}{stmt}\n" + "".join(lines[span.last_line :])
    ast.parse(stubbed)
    return stubbed


def _copy_scratch(scratch: Path) -> None:
    """Copy tracked plus untracked-but-not-ignored files of the repository into ``scratch``."""
    names = diffscope.git_output("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    for name in filter(None, names):
        src = diffscope.REPO_ROOT / name
        if src.is_file():
            (scratch / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, scratch / name, follow_symlinks=False)


_MEMORY = -9999  # pseudo exit code: the group was killed for exceeding its RSS cap
_POLL_SECONDS = 0.1


def _mem_floor() -> int:
    return int(os.environ.get("DEADCODE_STUB_MEM_FLOOR_MB", "2048")) << 20


def _group_rss(pgid: int) -> int:
    """Summed resident set (bytes) of process group ``pgid`` (RLIMIT_AS is not enforced on macOS, so poll ps)."""
    out = subprocess.run(["ps", "-axo", "pgid=,rss="], capture_output=True, text=True, check=False).stdout
    total = 0
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(pgid):
            total += int(fields[1]) * 1024
    return total


def _run(
    cmd: list[str], cwd: Path, env: dict[str, str], timeout: float, mem_cap: int | None = None, peak: list[int] | None = None
) -> tuple[int | None, str]:
    """Run a command in its own process group; ``None`` as code means it timed out, ``_MEMORY`` that its RSS passed ``mem_cap``."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True
    ) as proc:
        while True:
            try:
                out, _ = proc.communicate(timeout=_POLL_SECONDS)
                return proc.returncode, out
            except subprocess.TimeoutExpired:  # ai-slop: ignore[swallowed-exception] poll-loop idiom: timeout means still running
                pass
            rss = _group_rss(proc.pid)
            if peak is not None:
                peak[0] = max(peak[0], rss)
            exceeded = mem_cap is not None and rss > mem_cap
            if exceeded or time.monotonic() >= deadline:
                os.killpg(proc.pid, signal.SIGKILL)
                out, _ = proc.communicate()
                return (_MEMORY if exceeded else None), out


def _chunks(items: list[str], max_chars: int = _MAX_ARGV_CHARS) -> list[list[str]]:
    """Split node ids so that no single command line exceeds ``max_chars`` of arguments."""
    chunks: list[list[str]] = [[]]
    size = 0
    for item in items:
        if chunks[-1] and size + len(item) + 1 > max_chars:
            chunks.append([])
            size = 0
        chunks[-1].append(item)
        size += len(item) + 1
    return chunks


def _run_chunks(plain: list[str], tests: list[str], cwd: Path, env: dict[str, str], timeout: float, mem_cap: int | None = None):
    """Run ``tests`` in argv-sized chunks; stop at the first chunk that fails or times out."""
    deadline = time.monotonic() + timeout
    code, out = 0, ""
    for chunk in _chunks(tests):
        code, out = _run([*plain, *chunk], cwd, env, max(deadline - time.monotonic(), 1.0), mem_cap)
        if code != 0:
            break
    return code, out


def _covering_tests(data_file: Path, scratch: Path, path: str, spans: list[FunctionSpan]) -> dict[str, list[str]] | None:
    from coverage import CoverageData

    data = CoverageData(basename=str(data_file))
    data.read()
    wanted = os.path.realpath(scratch / path)
    contexts: dict[int, list[str]] = {}
    for measured in data.measured_files():
        # relative_files = True in a target's coverage config stores paths relative to the scratch root
        if os.path.realpath(scratch / measured) == wanted:
            contexts = data.contexts_by_lineno(measured)
    if not any(c for ctxs in contexts.values() for c in ctxs):
        return None
    return {
        span.locator: sorted(
            {c for line in range(span.first_line, span.last_line + 1) for c in contexts.get(line, []) if c}
        )
        for span in spans
    }


def run_audit(paths: list[Path]) -> Report:
    """Stub every function of ``paths`` (repo-relative) in a scratch copy and report detection."""
    try:
        import coverage  # noqa: F401
    except ImportError as exc:
        raise HarnessError("coverage is not importable; install the mutation extra (uv sync --extra mutation)") from exc
    raw: dict[Path, bytes] = {}
    sources: dict[Path, str] = {}
    for path in paths:
        try:
            raw[path] = (diffscope.REPO_ROOT / path).read_bytes()
            sources[path] = raw[path].decode("utf-8-sig")
            find_functions(sources[path])
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            raise HarnessError(f"cannot parse {path}: {exc}") from exc
    roots = mutation.build_mutation_test_roots(paths)
    scratch_parent = Path(tempfile.mkdtemp(prefix="deadcode-stub-"))
    try:
        scratch = scratch_parent / "tree"
        scratch.mkdir()
        _copy_scratch(scratch)
        scratch = scratch.resolve()
        plugin_dir = scratch_parent / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "stub_cov_plugin.py").write_text(_PLUGIN, encoding="utf-8")
        data_file = scratch_parent / "stub.coverage"
        source_dirs = [str(scratch / r) for r in diffscope.source_roots()]
        env = dict(
            os.environ,
            DEADCODE_REPO_ROOT=str(scratch),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=os.pathsep.join([str(plugin_dir), *source_dirs, str(scratch), os.environ.get("PYTHONPATH", "")]),
            STUB_COV_DATA=str(data_file),
            STUB_COV_SOURCE=str(scratch),
        )
        base = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "stub_cov_plugin"]
        started = time.monotonic()
        peak = [0]
        code, out = _run([*base, *roots], scratch, env, timeout=3600, peak=peak)
        elapsed = time.monotonic() - started
        if code != 0:
            tail = "\n".join(out.strip().splitlines()[-15:])
            raise HarnessError(f"baseline red (pytest {'timeout' if code is None else f'exit {code}'}):\n{tail}")
        timeout = max(10.0, 3 * elapsed)
        mem_cap = max(_mem_floor(), 4 * peak[0])
        report = Report()
        empty_files: list[str] = []
        run_env = {k: v for k, v in env.items() if k != "STUB_COV_DATA"}
        plain = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"]
        for path in paths:
            spans, skipped = find_functions(sources[path])
            report.skipped.extend({"file": path.as_posix(), **s} for s in skipped)
            covering = _covering_tests(data_file, scratch, path.as_posix(), spans)
            if covering is None:
                empty_files.append(path.as_posix())
                continue
            target = scratch / path
            for span in spans:
                stubbed = _stub_one(plain, run_env, scratch, target, raw[path], path, span, covering[span.locator], timeout, mem_cap)
                if stubbed is None:
                    report.skipped.append({"file": path.as_posix(), "function": span.locator, "reason": "stub does not parse"})
                else:
                    report.results.append(stubbed)
        if empty_files and len(empty_files) == len(paths):
            raise HarnessError("coverage mapping empty: check source roots (no test line coverage in any target file)")
        return report
    finally:
        shutil.rmtree(scratch_parent, ignore_errors=True)


def _stub_one(
    plain: list[str], env: dict[str, str], scratch: Path, target: Path, raw: bytes, path: Path,
    span: FunctionSpan, tests: list[str], timeout: float, mem_cap: int,
) -> StubResult | None:
    """Stub one function and run its covering tests; ``None`` when the stubbed source does not parse."""
    def result(detected: bool, by: str | None, reason: str | None = None, seconds: float = 0.0) -> StubResult:
        return StubResult(path.as_posix(), span.locator, span.stub_kind, detected, by, len(tests), seconds, reason)

    if not tests:
        return result(False, None, "not covered")
    try:
        stubbed = stub_source(raw.decode("utf-8-sig"), span)
    except SyntaxError:
        return None
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
    target.write_bytes(bom + stubbed.encode("utf-8"))
    started = time.monotonic()
    try:
        code, out = _run_chunks(plain, tests, scratch, env, timeout, mem_cap)
    finally:
        target.write_bytes(raw)
    seconds = round(time.monotonic() - started, 3)
    if code is None:
        return result(True, "timeout", seconds=seconds)
    if code == _MEMORY:
        return result(True, "memory", seconds=seconds)
    if code == 0:
        return result(False, None, "no covering test failed", seconds)
    if code == 1:
        return result(True, "test failure", seconds=seconds)
    if code == 2 or (code == 4 and any(marker in out for marker in _COLLECTION_ERROR_MARKERS)):
        # pytest -x turns a collection/import error caused by the stub into exit 4 on some versions
        return result(True, "crash", seconds=seconds)
    tail = "\n".join(out.strip().splitlines()[-10:])
    raise HarnessError(f"pytest exit {code} while stubbing {path}::{span.locator}:\n{tail}")


def select_paths(explicit: list[str], compare_branch: str) -> list[Path]:
    """Explicit files, or changed runtime files vs ``compare_branch`` (same selection as the mutation gate)."""
    if not explicit:
        changed = diffscope.changed_python_files(compare_branch)
        return mutation.build_mutation_targets(changed, compare_branch=compare_branch)
    paths = []
    for raw in explicit:
        resolved = Path(raw).resolve()
        if not resolved.is_relative_to(diffscope.REPO_ROOT) or not resolved.is_file():
            raise HarnessError(f"not a file inside the repository: {raw}")
        paths.append(resolved.relative_to(diffscope.REPO_ROOT))
    return sorted(dict.fromkeys(paths))


def run(explicit: list[str], compare_branch: str, *, as_json: bool) -> int:
    paths = select_paths(explicit, compare_branch)
    if not paths:
        print("No files to audit.", file=sys.stderr)
        return 0
    try:
        report = run_audit(paths)
    except HarnessError as exc:
        print(f"deadcode-audit error: stub-sensitivity: {exc}", file=sys.stderr)
        return 2
    undetected = [r for r in report.results if not r.detected]
    if as_json:
        summary = {"stubbed": len(report.results), "undetected": len(undetected), "skipped": len(report.skipped)}
        print(json.dumps({"results": [asdict(r) for r in report.results], "skipped": report.skipped, "summary": summary}, indent=2))
    else:
        for r in undetected:
            print(f"{r.file}::{r.function}  [{r.stub_kind}] not detected: {r.reason} (tests run: {r.tests_run})")
        print(f"{len(report.results)} stubbed, {len(undetected)} undetected (pseudo-tested or dead), {len(report.skipped)} skipped. Advisory leads, not proof.")
    return 0

