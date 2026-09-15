"""Tests for the dead-code module (``deadcode_audit``): path selection, gates, CLI dispatch.

Cross-module helpers are monkeypatched in the submodule where they are *used*
(``diffscope.added_lines_by_file``, ``vulture_gate._run_vulture``), which is where the
package binds them.
"""

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from deadcode_audit import cli, diffscope, mutation, reachability, vulture_gate
from deadcode_audit.clones import CloneFinding  # noqa: F401  (kept importable for downstream tests)
from deadcode_audit.mutation import (
    MutationGateStat,
    build_also_copy,
    build_mutation_copy_roots,
    build_mutation_targets,
    build_mutation_test_roots,
    build_mypy_targets,
    mutation_gate_exit_code,
    mutation_gate_failures,
    purge_cached_test_modules,
    reap_next_mutant_child,
)
from deadcode_audit.reachability import (
    PublicSymbolDefinition,
    symbols_without_runtime_consumers,
)
from deadcode_audit.vulture_gate import (
    VultureFinding,
    filter_findings_to_changed_lines,
    parse_vulture_findings,
    split_blocking_advisory,
)

@pytest.fixture(autouse=True)
def configured_project(tmp_path, monkeypatch):
    """The legacy client layout is an explicit client fixture, not package policy."""
    import yaml
    project = {
        "source_roots": ["src", "scripts"],
        "import_roots": ["src", "."],
        "test_roots": ["tests/unit", "tests/scripts"],
        "mutation_roots": ["src/modules", "scripts/deadcode", "scripts/evals/monorepo_eval.py"],
        "mutation_also_copy": ["scripts", "support", "experiments", "telegram_allowlist.txt", "evals/scenarios", "evals/rubrics", "evals/feedback", "evals/deadcode", "docs/progress_logs/_evidence/scripts", "docs/manual", "docs/diagram_targets.yml", ".env.example", ".pre-commit-config.yaml", "main.py"],
        "mutation_test_map": {"src/modules/evals": ["tests/unit/evals"], "scripts/evals/monorepo_eval.py": ["tests/unit/evals"], "scripts/deadcode": ["tests/scripts"]},
    }
    (tmp_path / ".deadcode.yml").write_text(yaml.safe_dump({"project": project}))
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)


# A file inside the dead-code module: the mutation gate's generalised self-target.
_SELF_TARGET = Path("scripts/deadcode/mutation.py")


def _mutation_stat(**overrides: int) -> MutationGateStat:
    """Build a default mutation summary for gate tests."""
    return replace(
        MutationGateStat(
            killed=3,
            survived=0,
            no_tests=0,
            timeout=0,
            suspicious=0,
            skipped=0,
            not_checked=0,
            check_was_interrupted_by_user=0,
            segfault=0,
        ),
        **overrides,
    )


def test_build_mypy_targets_keeps_typed_runtime_files() -> None:
    """The mypy dead-branch gate should keep only typed runtime targets."""
    changed_files = [
        Path("src/modules/evals/paths.py"),
        Path("scripts/evals/monorepo_eval.py"),
        _SELF_TARGET,
        Path("scripts/debug_task_query.py"),
        Path("tests/scripts/test_dead_code_guard.py"),
    ]

    assert build_mypy_targets(changed_files) == [
        _SELF_TARGET,
        Path("scripts/evals/monorepo_eval.py"),
        Path("src/modules/evals/paths.py"),
    ]


def test_build_mutation_targets_excludes_tests_and_auxiliary_scripts() -> None:
    """The mutation gate should focus on runtime code only."""
    changed_files = [
        Path("src/modules/evals/paths.py"),
        Path("scripts/evals/monorepo_eval.py"),
        _SELF_TARGET,
        Path("tests/scripts/test_dead_code_guard.py"),
    ]

    assert build_mutation_targets(changed_files) == [
        _SELF_TARGET,
        Path("scripts/evals/monorepo_eval.py"),
        Path("src/modules/evals/paths.py"),
    ]


def test_build_mutation_copy_roots_keeps_runnable_source_tree() -> None:
    """Mutation runs should copy broad roots while mutating only changed files."""
    targets = [
        Path("src/modules/evals/paths.py"),
        Path("scripts/evals/monorepo_eval.py"),
    ]

    assert build_mutation_copy_roots(targets) == [
        Path("scripts/evals/monorepo_eval.py"),
        Path("src/modules"),
    ]


def test_build_mutation_copy_roots_include_scripts_for_guard_only_changes() -> None:
    """Guard-only mutations still need the scripts tree copied into the mutmut sandbox."""
    targets = [_SELF_TARGET]

    assert build_mutation_copy_roots(targets) == [Path("scripts/deadcode")]


def test_build_also_copy_adds_scripts_for_src_only_diff() -> None:
    """A src-only diff must still copy ``scripts/`` into mutants/: tests/unit/scripts/*
    load repo-root scripts via __file__-relative paths that resolve into mutants/
    (was: clean-test pass aborted with FileNotFoundError on every src-only PR)."""
    result = build_also_copy([Path("setup.cfg")], [Path("src/modules")])

    assert Path("scripts") in result
    # CLI tests spec-load repo-root main.py via __file__-relative paths too.
    assert Path("main.py") in result
    # Diagrammer/size-guard tests load the autodocs diagram config at setup.
    assert Path("docs/diagram_targets.yml") in result
    assert Path("setup.cfg") in result  # mutmut's own defaults are preserved


def test_build_also_copy_skips_scripts_when_already_a_copy_root() -> None:
    """When scripts/ is a mutation copy root it must not be double-copied via also_copy."""
    result = build_also_copy([], [Path("scripts")])

    assert Path("scripts") not in result


def test_purge_cached_test_modules_drops_test_modules_keeps_src() -> None:
    """Between in-process pytest passes, cached test modules must be dropped so a
    src-module reload in pass N can't poison exception-class identity in pass N+1;
    src modules stay cached (reload updates them in place)."""
    from types import ModuleType

    tests_pkg = ModuleType("tests")
    top_level_leaf = ModuleType("test_config_layered")
    top_level_leaf.__file__ = "/repo/mutants/tests/unit/core/test_config_layered.py"
    fixtures = ModuleType("tests.fixtures.health_repo")
    src_module = ModuleType("modules.core.config")
    src_module.__file__ = "/repo/mutants/src/modules/core/config.py"
    prefix_collision = ModuleType("tests_helpers")
    prefix_collision.__file__ = "/repo/src/tests_helpers.py"
    modules = {
        "tests": tests_pkg,
        "test_config_layered": top_level_leaf,
        "tests.fixtures.health_repo": fixtures,
        "modules.core.config": src_module,
        "tests_helpers": prefix_collision,
    }

    purged = purge_cached_test_modules(modules)

    assert sorted(purged) == ["test_config_layered", "tests", "tests.fixtures.health_repo"]
    assert set(modules) == {"modules.core.config", "tests_helpers"}


def test_reap_next_mutant_child_skips_stray_children() -> None:
    """A zombie leaked by an in-process test pass must be reaped-and-skipped, not
    fed into mutmut's pid ledger (bare os.wait() KeyError'd the per-mutant phase)."""
    exits = iter([(111, 0), (222, 7 << 8)])
    mutant_children = {222}

    pid, wait_status = reap_next_mutant_child(lambda: next(exits), mutant_children)

    assert (pid, wait_status) == (222, 7 << 8)  # stray 111 dropped, mutant pid returned
    assert mutant_children == set()  # returned pid removed from the ledger


def test_reap_next_mutant_child_propagates_no_children_left() -> None:
    """ChildProcessError from the real wait (no children at all) must propagate —
    mutmut's run loop handles it as the normal end-of-children signal."""

    def _no_children() -> tuple[int, int]:
        raise ChildProcessError

    with pytest.raises(ChildProcessError):
        reap_next_mutant_child(_no_children, set())


def test_patch_setproctitle_noop_replaces_cf_touching_title_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """mutmut sets a process title inside every forked mutant child; on macOS that
    routes through CoreFoundation, which is fork-unsafe after the in-process test
    passes → all 9427 children SIGSEGV'd. The gate must no-op the title call."""
    mm = pytest.importorskip("mutmut.__main__")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("real setproctitle must not be reachable during the gate run")

    monkeypatch.setattr(mm, "setproctitle", _boom)
    mutation._patch_setproctitle_noop()

    assert mm.setproctitle("mutmut: some-mutant") is None  # boom not called → patched


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system-proxy lookup only exists on darwin")
def test_patch_macos_proxy_lookup_stubs_cf_backed_getproxies() -> None:
    """urllib's getproxies_macosx_sysconf goes through SystemConfiguration/CF —
    fork-unsafe in mutant children (16 SIGSEGVs when a test built an httpx
    client). The gate must pin the lookup to an empty proxy map."""
    import urllib.request

    original = urllib.request.getproxies_macosx_sysconf
    try:
        mutation._patch_macos_proxy_lookup()
        assert urllib.request.getproxies_macosx_sysconf() == {}
    finally:
        urllib.request.getproxies_macosx_sysconf = original


def test_build_mutation_test_roots_prefers_eval_slice() -> None:
    """Eval mutations should run against the focused eval test slice, not the whole unit suite."""
    targets = [
        Path("src/modules/evals/paths.py"),
        Path("scripts/evals/monorepo_eval.py"),
    ]

    assert build_mutation_test_roots(targets) == ["tests/unit/evals"]


def test_build_mutation_test_roots_includes_script_tests_for_guard_changes() -> None:
    """Guard mutations should run the script test slice that exercises the dead-code module."""
    targets = [
        _SELF_TARGET,
        Path("scripts/evals/monorepo_eval.py"),
    ]

    assert build_mutation_test_roots(targets) == ["tests/scripts", "tests/unit/evals"]


def test_mutation_gate_passes_when_only_kills_and_skips_exist() -> None:
    """The mutation gate should pass when no blocking mutation statuses remain."""
    stat = _mutation_stat(skipped=2)

    assert mutation_gate_failures(stat) == []
    assert mutation_gate_exit_code(stat) == 0


def test_mutation_gate_fails_below_kill_rate_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutation score below the floor must fail the gate."""
    monkeypatch.delenv("DEADCODE_MUTMUT_MIN_KILL_RATE", raising=False)
    stat = _mutation_stat(killed=1, survived=1)  # 0.500 < 0.70 default floor

    assert mutation_gate_failures(stat) == ["kill_rate=0.500<floor=0.70 (survived=1)"]
    assert mutation_gate_exit_code(stat) == 1


def test_mutation_gate_tolerates_survivors_at_kill_rate_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Survivors are tolerated while the mutation score stays at/above the floor —
    zero-survivor over whole changed files was unreachable (survivors land on
    pre-existing lines of touched files)."""
    monkeypatch.delenv("DEADCODE_MUTMUT_MIN_KILL_RATE", raising=False)
    stat = _mutation_stat(killed=7, survived=3)  # 0.70 == default floor

    assert mutation_gate_failures(stat) == []
    assert mutation_gate_exit_code(stat) == 0


def test_mutation_gate_kill_rate_floor_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor is config-driven: a stricter env floor turns a passing score red."""
    monkeypatch.setenv("DEADCODE_MUTMUT_MIN_KILL_RATE", "0.9")
    stat = _mutation_stat(killed=8, survived=2)  # 0.800 < 0.9

    assert mutation_gate_exit_code(stat) == 1


def test_min_kill_rate_rejects_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed floor must fail loud, not silently gate at a wrong threshold."""
    monkeypatch.setenv("DEADCODE_MUTMUT_MIN_KILL_RATE", "1.5")

    with pytest.raises(ValueError, match="DEADCODE_MUTMUT_MIN_KILL_RATE"):
        mutation.min_kill_rate()


def test_kill_rate_with_no_checked_mutants_is_full() -> None:
    """No checked mutants (e.g. everything skipped) must not divide by zero or fail."""
    stat = _mutation_stat(killed=0, skipped=2)

    assert mutation.kill_rate(stat) == 1.0


def test_mutation_gate_fails_for_other_blocking_statuses() -> None:
    """Harness-health categories stay zero-tolerance regardless of kill rate."""
    stat = _mutation_stat(
        no_tests=2,
        timeout=1,
        suspicious=1,
        check_was_interrupted_by_user=1,
        not_checked=1,
        segfault=1,
    )

    assert mutation_gate_failures(stat) == [
        "no_tests=2",
        "timeout=1",
        "suspicious=1",
        "not_checked=1",
        "check_was_interrupted_by_user=1",
        "segfault=1",
    ]
    assert mutation_gate_exit_code(stat) == 1


def test_symbols_without_runtime_consumers_reports_unreferenced_symbols() -> None:
    """A public symbol without runtime references should fail the consumer gate."""
    # Synthetic symbol/location (not a real module symbol) — this unit test pins the dedup logic of
    # symbols_without_runtime_consumers, so it must not couple to whether a particular src symbol exists.
    definition = PublicSymbolDefinition(
        file_path=Path("src/modules/example/widget.py"),
        symbol="unreferenced_widget",
        line=42,
    )

    missing = symbols_without_runtime_consumers(
        [definition],
        {"unreferenced_widget": [(Path("src/modules/example/widget.py"), 42)]},
    )

    assert missing == [definition]


def test_symbols_without_runtime_consumers_accepts_cross_file_usage() -> None:
    """A public symbol referenced by another runtime module should pass the gate."""
    definition = PublicSymbolDefinition(
        file_path=Path("src/modules/evals/paths.py"),
        symbol="resolve_run_paths",
        line=128,
    )

    missing = symbols_without_runtime_consumers(
        [definition],
        {
            "resolve_run_paths": [
                (Path("src/modules/evals/paths.py"), 128),
                (Path("scripts/evals/monorepo_eval.py"), 92),
            ]
        },
    )

    assert missing == []


def test_symbols_without_runtime_consumers_accepts_same_file_runtime_usage() -> None:
    """A symbol referenced from another line in the same module counts as consumed."""
    definition = PublicSymbolDefinition(
        file_path=Path("src/modules/evals/paths.py"),
        symbol="SnapshotPaths",
        line=35,
    )

    missing = symbols_without_runtime_consumers(
        [definition],
        {
            "SnapshotPaths": [
                (Path("src/modules/evals/paths.py"), 35),
                (Path("src/modules/evals/paths.py"), 101),
            ]
        },
    )

    assert missing == []


def test_parse_vulture_findings_extracts_path_line_message_confidence() -> None:
    """Vulture report lines should parse into structured findings, ignoring noise."""
    output = (
        "src/modules/a.py:12: unreachable code after 'return' (100% confidence)\n"
        "src/modules/b.py:7: unused import 'os' (90% confidence)\n"
        "src/modules/c.py:40: unused method 'helper' (60% confidence)\n"
        "\n"
        "Some unrelated summary line that is not a finding\n"
    )

    findings = parse_vulture_findings(output)

    assert findings == [
        VultureFinding(Path("src/modules/a.py"), 12, "unreachable code after 'return'", 100),
        VultureFinding(Path("src/modules/b.py"), 7, "unused import 'os'", 90),
        VultureFinding(Path("src/modules/c.py"), 40, "unused method 'helper'", 60),
    ]


def test_parse_added_lines_covers_single_multi_newfile_and_deletion_hunks() -> None:
    """The -U0 hunk parser should map every added/modified new-side line."""
    diff_text = "\n".join(
        [
            "diff --git a/src/x.py b/src/x.py",
            "--- a/src/x.py",
            "+++ b/src/x.py",
            "@@ -1 +1 @@",  # single-line modify -> {1}
            "@@ -0,0 +5,3 @@",  # new block of 3 lines -> {5,6,7}
            "@@ -10,2 +9,0 @@",  # pure deletion (count 0) -> {}
            "@@ -20,3 +18,2 @@ def foo():",  # 2 lines with trailing context -> {18,19}
        ]
    )

    assert diffscope.parse_added_lines(diff_text) == {1, 5, 6, 7, 18, 19}


def test_parse_added_lines_empty_diff_returns_empty_set() -> None:
    """A diff with no hunks (e.g. mode-only change) yields no changed lines."""
    assert diffscope.parse_added_lines("") == set()


def test_filter_findings_to_changed_lines_keeps_only_touched_definitions() -> None:
    """Only findings on lines the branch added or modified should survive."""
    findings = [
        VultureFinding(Path("src/modules/a.py"), 12, "unreachable code after 'return'", 100),
        VultureFinding(Path("src/modules/a.py"), 99, "unused function 'old'", 60),
        VultureFinding(Path("src/modules/untouched.py"), 3, "unused import 'sys'", 90),
    ]
    changed_lines_by_file = {"src/modules/a.py": {12, 13}}

    kept = filter_findings_to_changed_lines(findings, changed_lines_by_file)

    assert kept == [VultureFinding(Path("src/modules/a.py"), 12, "unreachable code after 'return'", 100)]


def test_split_blocking_advisory_partitions_at_threshold() -> None:
    """Confidence at or above the threshold blocks; below it is advisory only."""
    findings = [
        VultureFinding(Path("src/a.py"), 1, "unreachable code", 100),
        VultureFinding(Path("src/a.py"), 2, "unused import 'os'", 80),
        VultureFinding(Path("src/a.py"), 3, "unused method 'helper'", 79),
        VultureFinding(Path("src/a.py"), 4, "unused function 'old'", 60),
    ]

    blocking, advisory = split_blocking_advisory(findings, 80)

    assert [finding.confidence for finding in blocking] == [100, 80]
    assert [finding.confidence for finding in advisory] == [79, 60]


def test_changed_src_python_files_keeps_configured_runtime_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Vulture gate scans all configured runtime roots."""
    monkeypatch.setattr(
        diffscope,
        "changed_python_files",
        lambda compare_branch: [
            Path("src/modules/a.py"),
            Path("scripts/tool.py"),
            Path("tests/test_a.py"),
            Path("src/modules/b.py"),
        ],
    )

    assert diffscope.changed_src_python_files("main") == [
        Path("src/modules/a.py"),
        Path("scripts/tool.py"),
        Path("src/modules/b.py"),
    ]


def test_added_lines_by_file_maps_each_path_to_its_changed_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each changed file maps to the union of its -U0 hunk line numbers."""
    monkeypatch.setattr(diffscope, "_resolve_compare_ref", lambda compare_branch: "origin/main")

    def fake_git_output(*args: str) -> str:
        if args[0] == "merge-base":
            return "abc123"
        if args[0] == "diff":
            return "@@ -0,0 +1,2 @@\n@@ -5 +8 @@"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(diffscope, "_git_output", fake_git_output)

    assert diffscope.added_lines_by_file("main", [Path("src/a.py")]) == {"src/a.py": {1, 2, 8}}


def test_run_vulture_returns_stdout_on_dead_code_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vulture exit 3 (dead code found) is a successful run; stdout is returned."""
    monkeypatch.setattr(
        vulture_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=3, stdout="src/a.py:1: unused import 'os' (90% confidence)\n", stderr=""
        ),
    )

    assert "unused import 'os'" in vulture_gate._run_vulture(60)


def test_run_vulture_returns_stdout_on_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vulture exit 0 (no dead code) returns its (empty) stdout without raising."""
    monkeypatch.setattr(
        vulture_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert vulture_gate._run_vulture(60) == ""


def test_run_vulture_surfaces_parse_errors_on_success_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On exit 3 with stderr (a file Vulture couldn't parse), the warning is surfaced, not swallowed."""
    monkeypatch.setattr(
        vulture_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=3,
            stdout="src/a.py:1: unused import 'os' (90% confidence)\n",
            stderr="src/bad.py:1: invalid syntax",
        ),
    )

    assert "unused import 'os'" in vulture_gate._run_vulture(60)
    err = capsys.readouterr().err
    assert "could not analyze" in err
    assert "src/bad.py:1: invalid syntax" in err


def test_run_vulture_raises_on_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-(0,3) exit (syntax/encoding error, bad usage) is surfaced, not swallowed."""
    monkeypatch.setattr(
        vulture_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="x.py:1: invalid syntax"),
    )

    with pytest.raises(RuntimeError, match="Vulture failed"):
        vulture_gate._run_vulture(60)


def test_vulture_changed_check_skips_when_no_changed_src(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No changed src files short-circuits to exit 0 and never invokes Vulture."""
    monkeypatch.setattr(diffscope, "changed_src_python_files", lambda compare_branch: [])

    def _must_not_run(min_confidence: int) -> str:
        raise AssertionError("Vulture must not run when there are no changed src files")

    monkeypatch.setattr(vulture_gate, "_run_vulture", _must_not_run)

    assert vulture_gate.vulture_changed_check("main") == 0
    assert "No changed src files" in capsys.readouterr().out


def test_vulture_changed_check_blocks_on_high_confidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A >=80% finding on a changed line blocks (exit 1) and prints under [block]."""
    monkeypatch.setattr(diffscope, "changed_src_python_files", lambda compare_branch: [Path("src/a.py")])
    monkeypatch.setattr(diffscope, "added_lines_by_file", lambda compare_branch, paths: {"src/a.py": {3}})
    monkeypatch.setattr(
        vulture_gate,
        "_run_vulture",
        lambda min_confidence: "src/a.py:3: unreachable code after 'return' (100% confidence)\n",
    )

    assert vulture_gate.vulture_changed_check("main") == 1
    out = capsys.readouterr().out
    assert "[block]" in out
    assert "unreachable code after 'return'" in out


def test_vulture_changed_check_advisory_only_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 60% finding on a changed line is advisory only: printed but exit 0, no [block]."""
    monkeypatch.setattr(diffscope, "changed_src_python_files", lambda compare_branch: [Path("src/a.py")])
    monkeypatch.setattr(diffscope, "added_lines_by_file", lambda compare_branch, paths: {"src/a.py": {5}})
    monkeypatch.setattr(
        vulture_gate,
        "_run_vulture",
        lambda min_confidence: "src/a.py:5: unused function 'helper' (60% confidence)\n",
    )

    assert vulture_gate.vulture_changed_check("main") == 0
    out = capsys.readouterr().out
    assert "[advisory, non-blocking]" in out
    assert "[block]" not in out


def test_vulture_changed_check_ignores_findings_off_changed_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A high-confidence finding whose line was not changed does not block."""
    monkeypatch.setattr(diffscope, "changed_src_python_files", lambda compare_branch: [Path("src/a.py")])
    monkeypatch.setattr(diffscope, "added_lines_by_file", lambda compare_branch, paths: {"src/a.py": {1}})
    monkeypatch.setattr(
        vulture_gate,
        "_run_vulture",
        lambda min_confidence: "src/a.py:99: unreachable code after 'return' (100% confidence)\n",
    )

    assert vulture_gate.vulture_changed_check("main") == 0
    assert "[block]" not in capsys.readouterr().out


def test_public_symbol_definitions_flags_decorated_and_skips_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decorated public symbols are INCLUDED (decorated=True, not skipped); private names excluded.

    The old gate dropped all decorated symbols (silent pass for decorated-but-dead code). Now they
    are surfaced with a flag that only downgrades them to advisory, never grants a free pass.
    """
    monkeypatch.setattr(diffscope, "REPO_ROOT", tmp_path)
    module = tmp_path / "m.py"
    module.write_text(
        "@router.post('/p')\n"
        "def routed():\n    return 1\n\n"
        "def plain():\n    return 2\n\n"
        "def _private():\n    return 3\n",
        encoding="utf-8",
    )

    definitions = reachability._public_symbol_definitions(Path("m.py"))

    assert [(d.symbol, d.decorated) for d in definitions] == [("routed", True), ("plain", False)]


def test_precommit_vulture_ignore_names_match_config_ssot() -> None:
    """The pre-commit Vulture hook's --ignore-names must stay in sync with config (one SSOT).

    config.VULTURE_IGNORE_NAMES is the single source; this pins the .pre-commit-config.yaml
    literal to it so the two cannot silently drift.
    """
    from deadcode_audit import config

    repo_root = Path(__file__).resolve().parents[1] / "examples" / "monorepo"
    precommit = (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert f"--ignore-names {config.vulture_ignore_names_arg()}" in precommit


def test_main_dispatches_vulture_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI wires the vulture-changed subcommand to the gate with the given branch."""
    captured: dict[str, str] = {}

    def fake_check(compare_branch: str) -> int:
        captured["branch"] = compare_branch
        return 0

    monkeypatch.setattr(vulture_gate, "vulture_changed_check", fake_check)

    assert cli.main(["vulture-changed", "--compare-branch", "feature"]) == 0
    assert captured["branch"] == "feature"
