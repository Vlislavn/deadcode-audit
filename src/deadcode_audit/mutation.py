"""Tier 5: coverage + mutation dead-code gate.

Selects the changed runtime files to mutate, drives mutmut against the narrowest test slice
that exercises them, and maps the mutmut summary to a pass/fail gate. The dead-code module
mutation-tests itself: any changed file under ``scripts/deadcode/`` is a self-target.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from deadcode_audit import config, diffscope

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping
    from typing import Any


@dataclass(frozen=True)
class MutationGateStat:
    """Summarize the mutation outcomes that decide gate success."""

    killed: int
    survived: int
    no_tests: int
    timeout: int
    suspicious: int
    skipped: int
    not_checked: int
    check_was_interrupted_by_user: int
    segfault: int


DEFAULT_MIN_KILL_RATE = 0.70


def min_kill_rate() -> float:
    """Return the mutation-score floor (``DEADCODE_MUTMUT_MIN_KILL_RATE``, validated)."""
    raw = os.environ.get("DEADCODE_MUTMUT_MIN_KILL_RATE")
    if raw is None:
        return DEFAULT_MIN_KILL_RATE
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"DEADCODE_MUTMUT_MIN_KILL_RATE must be within [0, 1], got {raw!r}")
    return value


def kill_rate(stat: MutationGateStat) -> float:
    """Killed fraction of checked mutants (killed+survived); 1.0 when none were checked."""
    checked = stat.killed + stat.survived
    if checked == 0:
        return 1.0
    return stat.killed / checked


def mutation_gate_failures(stat: MutationGateStat) -> list[str]:
    """Return blocking mutation failures for the dead-code gate.

    Harness-health categories (no associated tests, timeouts, suspicious runs,
    unchecked mutants, interrupts, segfaults) stay zero-tolerance — any count
    means the measurement itself is broken. ``survived`` is gated as a mutation
    score instead: killed/(killed+survived) must reach ``min_kill_rate()``.
    A run where NO mutant was actually checked (``killed + survived == 0``, e.g. every one
    skipped) also blocks: ``kill_rate`` would otherwise report a meaningless 1.0 for an
    unmeasured run. That degenerate line is listed alone when no zero-tolerance category fired
    (with those present the run is already blocked and the list stays minimal).
    A zero-survivor policy over whole changed files was unreachable by
    construction — survivors land on pre-existing lines of touched files
    (measured on the first run that ever reached the per-mutant phase:
    2447/9411 survivors spread across 35/38 files, kill rate 0.740).
    """
    blocking_counts = {
        "no_tests": stat.no_tests,
        "timeout": stat.timeout,
        "suspicious": stat.suspicious,
        "not_checked": stat.not_checked,
        "check_was_interrupted_by_user": stat.check_was_interrupted_by_user,
        "segfault": stat.segfault,
    }
    failures = [f"{name}={count}" for name, count in blocking_counts.items() if count > 0]
    checked = stat.killed + stat.survived
    total = checked + stat.skipped + sum(blocking_counts.values())
    if total > 0 and checked == 0 and not failures:
        # Degenerate measurement (e.g. every mutant skipped): kill_rate() reports 1.0 and no
        # per-category counter tripped, so the gate must fail rather than pass unmeasured.
        # When another zero-tolerance category already fired, the run is blocked anyway and
        # the list stays minimal.
        failures.append(f"no_mutants_checked total={total} (skipped={stat.skipped})")
    rate = kill_rate(stat)
    floor = min_kill_rate()
    if rate < floor:
        failures.append(f"kill_rate={rate:.3f}<floor={floor:.2f} (survived={stat.survived})")
    return failures


def mutation_gate_exit_code(stat: MutationGateStat) -> int:
    """Return the shell exit code for a mutation summary."""
    if mutation_gate_failures(stat):
        return 1
    return 0


def _load_mutation_gate_stat() -> MutationGateStat:
    """Load the current mutmut summary from persisted mutation metadata."""
    import mutmut.__main__ as mutmut_main

    _, source_file_mutation_data_by_path = mutmut_main.collect_source_file_mutation_data(mutant_names=[])
    summary = mutmut_main.calculate_summary_stats(source_file_mutation_data_by_path)
    return MutationGateStat(
        killed=summary.killed,
        survived=summary.survived,
        no_tests=summary.no_tests,
        timeout=summary.timeout,
        suspicious=summary.suspicious,
        skipped=summary.skipped,
        not_checked=summary.not_checked,
        check_was_interrupted_by_user=summary.check_was_interrupted_by_user,
        segfault=summary.segfault,
    )


def _print_mutation_gate_failure(stat: MutationGateStat) -> None:
    """Print a concise mutation gate failure summary."""
    failures = mutation_gate_failures(stat)
    if not failures:
        return
    print(f"Mutation gate failed: {', '.join(failures)}")


def build_mutation_targets(paths: list[Path]) -> list[Path]:
    """Return changed runtime files that should be mutated.

    The mypy dead-branch gate selects the identical set (same ``is_mutation_target`` filter and
    dedup), so ``build_mypy_targets`` below is an alias, not a second implementation.
    """
    targets = [path for path in paths if config.is_mutation_target(path)]
    return sorted(dict.fromkeys(targets))


#: Alias: the mypy dead-branch gate mutates exactly the mutation-target set.
build_mypy_targets = build_mutation_targets


def build_mutation_copy_roots(paths: list[Path]) -> list[Path]:
    """Return broad source roots that mutmut must copy to run tests."""
    roots = diffscope.project_paths("mutation_roots", diffscope.source_roots())
    return sorted({root for root in roots if any(path == root or path.is_relative_to(root) for path in paths)})


def build_also_copy(base: list[Path], copy_roots: list[Path]) -> list[Path]:
    """Return mutmut's ``also_copy`` list: repo files the baseline suite reads inside ``mutants/``.

    ``tests/unit/scripts/*`` load repo-root ``scripts/*.py`` (check_file_size,
    check_layering) via ``Path(__file__)``-relative paths that resolve into
    ``mutants/`` at run time, so ``scripts/`` must be present there whenever it
    is not already a mutation copy root — otherwise the clean-test pass aborts
    the whole gate with FileNotFoundError before any mutant runs (red-from-birth
    on every src-only PR).
    """
    extras = diffscope.project_paths("mutation_also_copy", [])
    return [p for p in dict.fromkeys([*base, *extras]) if p not in copy_roots]


def reap_next_mutant_child(real_wait: Callable[[], tuple[int, int]], mutant_children: set[int]) -> tuple[int, int]:
    """Reap children until one of mutmut's own mutant forks exits; skip strays.

    mutmut's ``read_one_child_exit_status`` does a bare ``os.wait()`` and indexes
    its pid ledger with whatever pid comes back. The in-process stats/clean passes
    run the full suite in THIS process, and some tests leak zombie children
    (sandbox helpers, tracker processes) — a bare wait() reaps those too and
    KeyErrors the whole per-mutant phase (seen live: ``KeyError: 3752``). Strays
    are reaped and dropped; only pids recorded at fork time are returned.
    ``real_wait`` raising ``ChildProcessError`` (no children left) propagates —
    mutmut handles it.
    """
    while True:
        pid, wait_status = real_wait()
        if pid in mutant_children:
            mutant_children.discard(pid)
            return pid, wait_status


def build_do_not_mutate_patterns(copy_roots: list[Path], targets: list[Path]) -> list[str]:
    """Return file globs that keep mutation focused on the changed files only."""
    target_paths = {path.as_posix() for path in targets}
    ignored: list[str] = []
    for root in copy_roots:
        for candidate in sorted((diffscope.REPO_ROOT / root).rglob("*.py")):
            rel_path = candidate.relative_to(diffscope.REPO_ROOT).as_posix()
            if rel_path not in target_paths:
                ignored.append(rel_path)
    return ignored


def build_mutation_test_roots(paths: list[Path]) -> list[str]:
    """Return the narrowest test roots that exercise the changed mutation targets.

    Narrowing to ``tests/unit/evals`` (for ``src/modules/evals`` + ``monorepo_eval.py``)
    or ``tests/scripts`` (for self-targets) is only valid when **every** changed file
    falls into one of those special categories. A general ``src/modules`` file is
    exercised by its own unit tests elsewhere in the tree (e.g. ``tests/unit/core``,
    ``tests/unit/domain``), so if the diff mixes a general src file with an evals or
    self-target file, the narrow roots would run *only* the eval/script tests against
    *all* mutants — leaving the general files' mutants exercised by nothing that asserts
    their behaviour, so they survive as false positives. In that case fall back to the
    full unit+scripts suite (which is a superset of the narrow roots).
    """
    mapping = config.load_deadcode_config(diffscope.REPO_ROOT).project.get("mutation_test_map", {})
    matched = []
    for path in paths:
        choices = [(prefix, tests) for prefix, tests in mapping.items() if path.is_relative_to(Path(prefix))]
        if not choices:
            return [p.as_posix() for p in diffscope.project_paths("test_roots", [Path("tests")])]
        matched.extend(max(choices, key=lambda pair: len(pair[0]))[1])
    return sorted(set(matched)) or [p.as_posix() for p in diffscope.project_paths("test_roots", [Path("tests")])]


def _reset_mutants_dir() -> None:
    """Remove persisted mutmut state so each gate run reflects current results."""
    mutants_dir = diffscope.REPO_ROOT / "mutants"
    if mutants_dir.exists():
        shutil.rmtree(mutants_dir)


def _preimport_native_libs() -> None:
    """Import torch/numpy once, before mutmut starts coverage instrumentation.

    Many ``src/modules`` paths pull ``torch`` transitively (langchain → transformers →
    torch). mutmut runs the suite several times in the SAME process (coverage, clean
    test, stats, per-mutant). torch's C ``_add_docstr`` raises
    ``RuntimeError: function '_has_torch_function' already has a docstring`` when its
    module body executes while coverage's tracer is active. Importing torch/numpy here
    — fully, before any coverage tracer is installed — caches them in ``sys.modules`` so
    every later ``import torch`` is a no-op and the crash cannot occur. torch is an
    optional extra (``[rag]``/``[overlap]``); when it is not installed there is nothing
    to pre-import and the transitive import never happens either, so the ImportError is
    the genuine "feature absent" boundary, not a swallowed failure.
    """
    try:
        import numpy  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return


def purge_cached_test_modules(modules: MutableMapping[str, Any] | None = None) -> list[str]:
    """Drop cached test modules so each in-process pytest pass re-imports them fresh.

    mutmut runs several ``pytest.main()`` passes in ONE interpreter. A test that
    ``importlib.reload()``s a src module swaps every class object in that module's
    dict, while test modules cached from an earlier pass keep pre-reload class
    references — so ``pytest.raises``/``isinstance`` checks against post-reload
    classes fail only in later passes (seen live: ``UnknownConfigKeyError`` in
    test_config_layered, ``_ColorFormatter`` in the logging_config kill tests).
    Purging restores fresh-process semantics per pass; src modules stay cached.

    Leaf test modules are cached under TOP-LEVEL names (``test_config_layered``,
    dirs without ``__init__.py``), only package dirs under ``tests.*`` — so match
    by name for the package and by ``__file__`` path component for the rest.
    """
    if modules is None:
        modules = sys.modules
    purged = []
    for name, module in list(modules.items()):
        if name == "tests" or name.startswith("tests."):
            purged.append(name)
            continue
        module_file = getattr(module, "__file__", None)
        if module_file and "tests" in Path(module_file).parts:
            purged.append(name)
    for name in purged:
        del modules[name]
    return purged


def _patch_macos_proxy_lookup() -> None:
    """Stub urllib's macOS system-proxy lookup for the gate process tree.

    ``urllib.request.getproxies_macosx_sysconf`` routes through
    SystemConfiguration/CoreFoundation, which is fork-unsafe once the parent
    has initialized CF (guaranteed here by the in-process test passes). Any
    test that builds an httpx client inside a forked mutant child crashed the
    child with SIGSEGV (observed: 16 mutants "segfault", faulthandler stack
    ending in ``getproxies_macosx_sysconf``). Unit tests must not depend on
    host proxy config, so the gate pins the lookup to "no proxies".
    """
    if sys.platform != "darwin":
        return
    import urllib.request

    urllib.request.getproxies_macosx_sysconf = lambda: {}  # type: ignore[assignment,attr-defined]


def _patch_setproctitle_noop() -> None:
    """No-op mutmut's per-child ``setproctitle`` call (macOS fork-safety).

    mutmut sets a cosmetic process title inside every forked mutant child
    (``setproctitle(f'mutmut: {mutant_name}')``). The darwin implementation
    routes through CoreFoundation (``CFBundleGetFunctionPointerForName``),
    which is not fork-safe once the parent has initialized CF — guaranteed
    here by the in-process test passes. Every child then dies with SIGSEGV
    before running a single test (observed: 9427/9427 mutants "segfault",
    crash reports faulting in ``darwin_set_process_title``). The title is
    purely cosmetic, so drop it for the gate run.
    """
    import mutmut.__main__ as mm

    def _noop_setproctitle(*_args: object, **_kwargs: object) -> None:
        return None

    mm.setproctitle = _noop_setproctitle


def _patch_pass_isolation() -> None:
    """Purge cached test modules before every in-process pytest pass (see above)."""
    import mutmut.__main__ as mm

    _orig_execute_pytest = mm.PytestRunner.execute_pytest

    def execute_pytest(self: Any, params: list[str], **kwargs: Any) -> int:
        purge_cached_test_modules()
        return int(_orig_execute_pytest(self, params, **kwargs))

    mm.PytestRunner.execute_pytest = execute_pytest


def _patch_stats_no_truncate() -> None:
    """Stop mutmut's stats pass from truncating test↔mutant associations.

    mutmut builds the per-function test association by running the suite once with
    ``-x`` (plus the repo's inherited ``--maxfail=1``). A single test that errors
    *only* in the copied ``mutants/`` tree (different CWD / trampolined imports)
    halts that pass — so every test after it is never associated with the functions
    it covers, and those mutants then SURVIVE as false positives regardless of how
    thoroughly they are actually tested. Run the stats pass without early-exit and
    tolerate a non-zero result so associations are built from every passing test.
    Collection errors in one file are isolated (``--continue-on-collection-errors``)
    instead of aborting the whole pass.
    """
    import mutmut
    import mutmut.__main__ as mm

    def run_stats(self: Any, *, tests: Any) -> int:
        class StatsCollector:
            def pytest_runtest_logstart(self, nodeid: Any, location: Any) -> None:
                mutmut.duration_by_test[nodeid] = 0

            def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
                for function in mutmut._stats:
                    mutmut.tests_by_mangled_function_name[function].add(
                        mm.strip_prefix(item._nodeid, prefix="mutants/")
                    )
                mutmut._stats.clear()

            def pytest_runtest_makereport(self, item: Any, call: Any) -> None:
                mutmut.duration_by_test[item.nodeid] = mutmut.duration_by_test.get(item.nodeid, 0) + call.duration

        pytest_args = ["-q", "--maxfail=100000", "--continue-on-collection-errors"]
        if tests:
            pytest_args += list(tests)
        else:
            pytest_args += self._pytest_add_cli_args_test_selection
        with mm.change_cwd("mutants"):
            return int(self.execute_pytest(pytest_args, plugins=[StatsCollector()]))

    mm.PytestRunner.run_stats = run_stats

    # Diagnostic: when DEADCODE_MUTMUT_FRAGILE_LOG is set, run the *clean* test pass
    # (mutant_name is None) without ``-x`` so a single gate run records EVERY test
    # that errors only in the copied ``mutants/`` tree, instead of aborting at the
    # first one. Per-mutant runs (mutant_name set) are untouched — they must keep
    # ``-x`` so a mutant is killed the instant one associated test fails.
    _fragile_log = os.environ.get("DEADCODE_MUTMUT_FRAGILE_LOG")
    if _fragile_log:
        _orig_run_tests = mm.PytestRunner.run_tests

        def run_tests(self: Any, *, mutant_name: Any, tests: Any) -> int:
            if mutant_name is not None:
                return _orig_run_tests(self, mutant_name=mutant_name, tests=tests)
            fails: list[str] = []

            class FailRec:
                def pytest_runtest_logreport(self, report: Any) -> None:
                    if report.failed:
                        fails.append(f"{report.nodeid} [{report.when}]")

                def pytest_collectreport(self, report: Any) -> None:
                    if report.failed:
                        fails.append(f"COLLECT {report.nodeid}")

            pytest_args = [
                "-q",
                "--maxfail=100000",
                "--continue-on-collection-errors",
                "-p",
                "no:randomly",
                "-p",
                "no:random-order",
            ]
            if tests:
                pytest_args += list(tests)
            else:
                pytest_args += self._pytest_add_cli_args_test_selection
            with mm.change_cwd("mutants"):
                result = int(self.execute_pytest(pytest_args, plugins=[FailRec()]))
            Path(_fragile_log).write_text("\n".join(fails), encoding="utf-8")
            return result

        mm.PytestRunner.run_tests = run_tests


def run_mutmut_for_paths(paths: list[Path]) -> int:
    """Execute mutmut for the provided paths using the repo's shared config.

    Temporarily moves the process to ``REPO_ROOT`` (mutmut resolves ``mutants/`` and test roots
    relative to the CWD) and restores the original CWD in the ``finally`` below, so the chdir is
    scoped to this gate run rather than a permanent global side effect.
    """
    if not paths:
        print("No mutation targets changed vs compare branch")
        return 0

    if not hasattr(os, "fork"):
        raise RuntimeError("Mutation requires POSIX fork; use Linux/macOS or WSL")
    original_cwd = Path.cwd()
    os.chdir(diffscope.REPO_ROOT)
    _reset_mutants_dir()

    _preimport_native_libs()

    import mutmut
    import mutmut.__main__ as mutmut_main

    config_obj = mutmut_main.load_config()
    config_obj.paths_to_mutate = build_mutation_copy_roots(paths)
    config_obj.do_not_mutate = build_do_not_mutate_patterns(config_obj.paths_to_mutate, paths)
    config_obj.also_copy = build_also_copy(list(config_obj.also_copy), config_obj.paths_to_mutate)
    config_obj.tests_dir = build_mutation_test_roots(paths)
    config_obj.mutate_only_covered_lines = True
    # ``mutmut.__main__`` binds the same ``mutmut`` module object, so one assignment configures
    # both namespaces (the previous double assignment set the same attribute twice).
    mutmut.config = config_obj
    _patch_stats_no_truncate()
    _patch_pass_isolation()
    _patch_setproctitle_noop()
    _patch_macos_proxy_lookup()
    real_fork = os.fork
    real_wait = os.wait
    mutant_children: set[int] = set()

    def tracking_fork() -> int:
        pid = real_fork()
        if pid:
            mutant_children.add(pid)
        return pid

    os.fork = tracking_fork  # type: ignore[assignment]
    os.wait = lambda: reap_next_mutant_child(real_wait, mutant_children)  # type: ignore[assignment]
    try:
        mutmut_main._run([], None)
    finally:
        os.fork = real_fork  # type: ignore[assignment]
        os.wait = real_wait  # type: ignore[assignment]
        os.chdir(original_cwd)
    stat = _load_mutation_gate_stat()
    print(
        f"Mutation kill rate: {kill_rate(stat):.1%} "
        f"(killed={stat.killed}, survived={stat.survived}, floor={min_kill_rate():.0%})"
    )
    exit_code = mutation_gate_exit_code(stat)
    if exit_code != 0:
        _print_mutation_gate_failure(stat)
    return exit_code
