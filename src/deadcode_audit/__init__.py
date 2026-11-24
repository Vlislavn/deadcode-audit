"""Monorepo dead-code / AI-slop gate, as a cohesive module.

Layout (one concern each):

* :mod:`diffscope`, :mod:`layout`, :mod:`config` — git/changed-line primitives, repo layout, cross-tier constants.
* :mod:`structure`   — ``vulture_gate`` (Tier 2), ``reachability`` + ``resolver`` (Tier 4), ``redundancy`` (Tier 6a),
  ``cycles``, ``architecture``, ``reachability_scan``.
* :mod:`overlap`     — ``clones`` (Tier 6b), ``overlaps`` and their embedding/cache/blind-text helpers.
* :mod:`js`          — Oxlint/jscpd adapter and the Tree-sitter function collector.
* :mod:`gates`       — ``mutation`` (Tier 5), ``stub_sensitivity``, ``growth``, ``history``.
* :mod:`cli`         — ``python -m deadcode_audit <command>`` dispatch.

Public symbols are re-exported here for convenience; code that monkeypatches a helper
should target the submodule where it is *used* (e.g. ``diffscope.added_lines_by_file``).
"""

from __future__ import annotations

from deadcode_audit.cli import main
from deadcode_audit.diffscope import (
    added_lines_by_file,
    changed_python_files,
    changed_src_python_files,
    parse_added_lines,
)
from deadcode_audit.gates.mutation import (
    MutationGateStat,
    build_mutation_copy_roots,
    build_mutation_targets,
    build_mutation_test_roots,
    build_mypy_targets,
    mutation_gate_exit_code,
    mutation_gate_failures,
)
from deadcode_audit.overlap.clones import (
    CloneFinding,
    clone_similarity,
    collect_functions,
    find_near_duplicate_functions,
    normalize_function,
)
from deadcode_audit.structure.reachability import (
    PublicSymbolDefinition,
    runtime_consumer_check,
    symbols_without_runtime_consumers,
)
from deadcode_audit.structure.redundancy import (
    RedundancyFinding,
    find_redundant_transforms,
    run_check,
    run_files,
)
from deadcode_audit.structure.vulture_gate import (
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
