"""Monorepo dead-code / AI-slop gate, as a cohesive module.

Submodules (one concern each):

* :mod:`diffscope`   — shared git / merge-base / changed-line primitives.
* :mod:`config`      — single source of truth for cross-tier constants.
* :mod:`vulture_gate`  — Tier 2 diff-scoped Vulture reachability gate.
* :mod:`reachability`  — Tier 4 runtime-consumer check for changed public symbols.
* :mod:`mutation`      — Tier 5 coverage + mutation gate (and its target selection).
* :mod:`redundancy`    — Tier 6a redundant-transform detection + the Tier 6 orchestration.
* :mod:`clones`        — Tier 6b near-duplicate function detection.
* :mod:`cli`           — ``python -m deadcode_audit <command>`` dispatch.

Public symbols are re-exported here for convenience; code that monkeypatches a helper
should target the submodule where it is *used* (e.g. ``diffscope.added_lines_by_file``).
"""

from __future__ import annotations

from deadcode_audit.cli import main
from deadcode_audit.clones import (
    CloneFinding,
    clone_similarity,
    collect_functions,
    find_near_duplicate_functions,
    normalize_function,
)
from deadcode_audit.diffscope import (
    added_lines_by_file,
    changed_python_files,
    changed_src_python_files,
    parse_added_lines,
)
from deadcode_audit.mutation import (
    MutationGateStat,
    build_do_not_mutate_patterns,
    build_mutation_copy_roots,
    build_mutation_targets,
    build_mutation_test_roots,
    build_mypy_targets,
    mutation_gate_exit_code,
    mutation_gate_failures,
)
from deadcode_audit.reachability import (
    PublicSymbolDefinition,
    runtime_consumer_check,
    symbols_without_runtime_consumers,
)
from deadcode_audit.redundancy import (
    RedundancyFinding,
    find_redundant_transforms,
    run_check,
    run_files,
)
from deadcode_audit.vulture_gate import (
    VultureFinding,
    filter_findings_to_changed_lines,
    parse_vulture_findings,
    split_blocking_advisory,
    vulture_changed_check,
)

__all__ = [
    "CloneFinding",
    "MutationGateStat",
    "PublicSymbolDefinition",
    "RedundancyFinding",
    "VultureFinding",
    "added_lines_by_file",
    "build_do_not_mutate_patterns",
    "build_mutation_copy_roots",
    "build_mutation_targets",
    "build_mutation_test_roots",
    "build_mypy_targets",
    "changed_python_files",
    "changed_src_python_files",
    "clone_similarity",
    "collect_functions",
    "filter_findings_to_changed_lines",
    "find_near_duplicate_functions",
    "find_redundant_transforms",
    "main",
    "mutation_gate_exit_code",
    "mutation_gate_failures",
    "normalize_function",
    "parse_added_lines",
    "parse_vulture_findings",
    "run_check",
    "run_files",
    "runtime_consumer_check",
    "split_blocking_advisory",
    "symbols_without_runtime_consumers",
    "vulture_changed_check",
]
