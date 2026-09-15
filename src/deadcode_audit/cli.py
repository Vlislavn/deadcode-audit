"""Unified CLI for the dead-code module.

One ``argparse`` parser with a subcommand → handler registry (replacing the former
hand-written ``if args.command == ...`` chain that lived in two scripts). Entry point:
``python -m deadcode_audit <command>``.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from deadcode_audit import (
    config,
    diffscope,
    history,
    mutation,
    output,
    reachability,
    redundancy,
    vulture_gate,
)
from deadcode_audit import scan as scan_mod
from deadcode_audit.diagnostic import Severity

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _print_paths(paths: list[Path], *, null_terminated: bool = False) -> int:
    """Print relative paths using newline or NUL delimiters."""
    if null_terminated:
        for path in paths:
            sys.stdout.write(path.as_posix())
            sys.stdout.write("\0")
        return 0
    for path in paths:
        print(path.as_posix())
    return 0


def _cmd_changed_python_files(args: argparse.Namespace) -> int:
    return _print_paths(diffscope.changed_python_files(args.compare_branch), null_terminated=args.null)


def _cmd_mypy_targets(args: argparse.Namespace) -> int:
    return _print_paths(
        mutation.build_mypy_targets(diffscope.changed_python_files(args.compare_branch)),
        null_terminated=args.null,
    )


def _cmd_mutation_targets(args: argparse.Namespace) -> int:
    return _print_paths(
        mutation.build_mutation_targets(diffscope.changed_python_files(args.compare_branch)),
        null_terminated=args.null,
    )


def _cmd_run_mutmut_changed(args: argparse.Namespace) -> int:
    return mutation._run_mutmut_for_paths(
        mutation.build_mutation_targets(diffscope.changed_python_files(args.compare_branch))
    )


def _cmd_runtime_consumer_check(args: argparse.Namespace) -> int:
    return reachability.runtime_consumer_check(diffscope.changed_python_files(args.compare_branch))


def _cmd_vulture_changed(args: argparse.Namespace) -> int:
    return vulture_gate.vulture_changed_check(args.compare_branch)


def _cmd_redundancy(args: argparse.Namespace) -> int:
    if args.paths:
        return redundancy.run_files(args.paths)
    return redundancy.run_check(args.compare_branch, threshold=args.clone_threshold, min_tokens=args.min_tokens)


def _run_scan(args: argparse.Namespace) -> scan_mod.ScanResult:
    from deadcode_audit.detectors import ALL_DETECTORS

    cfg = config.load_deadcode_config(diffscope.REPO_ROOT)
    files = scan_mod.resolve_target_files(
        args.paths,
        changed=args.changed,
        compare_branch=args.compare_branch,
        exclude=cfg.exclude,
    )
    if not files and not args.changed:
        raise ValueError("No Python source files found; check target paths/source_roots")
    return scan_mod.run_scan(files, ALL_DETECTORS, cfg)


def _cmd_scan(args: argparse.Namespace) -> int:
    result = _run_scan(args)
    if args.json:
        print(output.render_json(result.diagnostics, result.score, files_scanned=result.file_count))
        return 0
    if args.sarif:
        print(output.render_sarif(result.diagnostics))
        return 0
    if args.prompt:
        print(output.render_agent_prompt(result.diagnostics))
        return 0
    print(output.render_terminal(result.diagnostics, result.score))
    if not args.no_history:
        from datetime import UTC, datetime

        diags = result.diagnostics
        history.append_record(
            history.HistoryRecord(
                timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
                score=result.score.score,
                label=result.score.label,
                errors=sum(1 for d in diags if d.severity is Severity.ERROR),
                warnings=sum(1 for d in diags if d.severity is Severity.WARNING),
                info=sum(1 for d in diags if d.severity is Severity.INFO),
                files=result.file_count,
            )
        )
    return 0


def _cmd_ci(args: argparse.Namespace) -> int:
    result = _run_scan(args)
    cfg = config.load_deadcode_config(diffscope.REPO_ROOT)
    fail_below = args.fail_below if args.fail_below is not None else (cfg.fail_below or 0)
    has_error = any(d.severity is Severity.ERROR for d in result.diagnostics)
    print(
        output.render_json(result.diagnostics, result.score, files_scanned=result.file_count)
        if args.json
        else output.render_terminal(result.diagnostics, result.score)
    )
    if has_error or result.score.score < fail_below:
        print(f"deadcode ci gate FAILED: score={result.score.score} failBelow={fail_below} errors={has_error}", file=sys.stderr)
        return 1
    return 0


def _cmd_rules(_args: argparse.Namespace) -> int:
    from deadcode_audit.detectors import ALL_DETECTORS
    from deadcode_audit.framework import all_rule_specs

    for spec in all_rule_specs(ALL_DETECTORS):
        style = " [style]" if spec.style else ""
        print(f"{spec.rule:<34} {spec.engine:<13} {spec.default_severity.value:<8} {spec.category}{style}")
    return 0


def _cmd_trend(args: argparse.Namespace) -> int:
    print(history.render_trend(history.read_records(), limit=args.limit))
    return 0


def _cmd_overlaps(args: argparse.Namespace) -> int:
    from deadcode_audit import overlaps  # lazy: keeps cli import torch-free

    return overlaps.run(
        threshold=args.threshold,
        top=args.top,
        min_tokens=args.min_tokens,
        no_embed=args.no_embed,
        model=args.model,
        as_json=args.json,
    )


def _cmd_reachability_scan(args: argparse.Namespace) -> int:
    from deadcode_audit import (
        reachability_scan,
    )  # lazy: parallels the other on-demand subcommands

    return reachability_scan.run(top=args.top, as_json=args.json)


def _cmd_cycles(args: argparse.Namespace) -> int:
    from deadcode_audit import cycles

    return cycles.run(as_json=args.json, advisory=args.advisory)


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "changed-python-files": _cmd_changed_python_files,
    "mypy-targets": _cmd_mypy_targets,
    "mutation-targets": _cmd_mutation_targets,
    "run-mutmut-changed": _cmd_run_mutmut_changed,
    "runtime-consumer-check": _cmd_runtime_consumer_check,
    "vulture-changed": _cmd_vulture_changed,
    "redundancy": _cmd_redundancy,
    "scan": _cmd_scan,
    "ci": _cmd_ci,
    "rules": _cmd_rules,
    "trend": _cmd_trend,
    "overlaps": _cmd_overlaps,
    "reachability-scan": _cmd_reachability_scan,
    "cycles": _cmd_cycles,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Python dead-code / quality audit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _with_compare(name: str, help_text: str, *, null: bool = False) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--compare-branch", default="main")
        if null:
            sub.add_argument("--null", action="store_true", help="Print NUL-delimited paths")
        return sub

    _with_compare("changed-python-files", "Print changed tracked Python files", null=True)
    _with_compare("mypy-targets", "Print changed files for the mypy dead-branch gate", null=True)
    _with_compare(
        "mutation-targets",
        "Print changed files for the mutation dead-code gate",
        null=True,
    )
    _with_compare("run-mutmut-changed", "Run mutmut against changed dead-code targets")
    _with_compare(
        "runtime-consumer-check",
        "Fail when changed public symbols have no runtime consumer",
    )
    _with_compare(
        "vulture-changed",
        "Vulture gate on changed lines (blocks; advises candidates)",
    )

    redundancy_parser = _with_compare(
        "redundancy",
        "Block redundant transforms; advise near-duplicate functions",
    )
    redundancy_parser.add_argument("--clone-threshold", type=float, default=0.95)
    redundancy_parser.add_argument("--min-tokens", type=int, default=40)
    redundancy_parser.add_argument(
        "paths",
        nargs="*",
        help="Explicit files to check for reducible transforms (pre-commit mode). "
        "When omitted, runs the full diff-scoped scan against --compare-branch.",
    )

    def _add_scan_args(parser_obj: argparse.ArgumentParser) -> None:
        parser_obj.add_argument("paths", nargs="*", help="Files/dirs to scan (default: configured roots)")
        parser_obj.add_argument("--compare-branch", default="main")
        parser_obj.add_argument(
            "--changed",
            action="store_true",
            help="Scan only changed src files vs --compare-branch",
        )

    scan_parser = subparsers.add_parser("scan", help="Scan Python files and score 0–100")
    _add_scan_args(scan_parser)
    scan_parser.add_argument("--json", action="store_true", help="JSON output")
    scan_parser.add_argument("--sarif", action="store_true", help="SARIF 2.1.0 output")
    scan_parser.add_argument("--prompt", action="store_true", help="Emit an agent hand-off prompt")
    scan_parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not append to the score-history trend file",
    )

    ci_parser = subparsers.add_parser("ci", help="Scan and gate (exit 1 on errors or low score)")
    _add_scan_args(ci_parser)
    ci_parser.add_argument("--json", action="store_true", help="JSON output")
    ci_parser.add_argument(
        "--fail-below",
        type=int,
        default=None,
        help="Override the failBelow score threshold",
    )

    subparsers.add_parser("rules", help="List detector rules")

    trend_parser = subparsers.add_parser("trend", help="Show score history")
    trend_parser.add_argument("--limit", type=int, default=20)

    # On-demand, advisory: full all-pairs semantic overlap audit (whole tree, NOT diff-scoped).
    overlaps_parser = subparsers.add_parser(
        "overlaps",
        help="All-pairs function-overlap audit (advisory)",
    )
    overlaps_parser.add_argument("--threshold", type=float, default=0.55)
    overlaps_parser.add_argument("--top", type=int, default=50, help="Keep the top N pairs (0 = all)")
    overlaps_parser.add_argument("--min-tokens", type=int, default=40)
    overlaps_parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip code embeddings; deterministic struct+api only",
    )
    overlaps_parser.add_argument(
        "--model",
        default="microsoft/codebert-base",
        help="HF code-embedding model id ([overlap] extra)",
    )
    overlaps_parser.add_argument("--json", action="store_true", help="JSON output")

    # On-demand, advisory: whole-tree dead-symbol reachability scan (every runtime public symbol, NOT diff-scoped).
    reachability_scan_parser = subparsers.add_parser(
        "reachability-scan",
        help="Whole-tree dead-symbol scan (advisory)",
    )
    reachability_scan_parser.add_argument(
        "--top",
        type=int,
        default=40,
        help="Max lower-confidence (decorated/unresolved) rows to show; orphaned + test-only always print (0 = all)",
    )
    reachability_scan_parser.add_argument("--json", action="store_true", help="JSON output (always the complete list)")

    # Import-cycle guard (Tarjan SCC over src/ modules). Blocks by default; --advisory for the whole-tree audit.
    cycles_parser = subparsers.add_parser(
        "cycles",
        help="Detect circular imports (blocks unless --advisory)",
    )
    cycles_parser.add_argument(
        "--advisory",
        action="store_true",
        help="Report only, always exit 0",
    )
    cycles_parser.add_argument("--json", action="store_true", help="JSON output")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the dead-code gate CLI."""
    args = _build_parser().parse_args(argv)
    return COMMANDS[args.command](args)
