# Changelog

## [Unreleased]

### Added
- `growth --base BASE --head HEAD`: size and churn of changed non-test Python files, ported from agent-skills. JSON includes counts, deltas, exclusions and AST-unknown locators. No model, checkout, score change or new blocking threshold.
- Mixed Python/JS/TS scan/CI and function overlaps with exact source identities (`javascript` extra; Oxlint 1.90.0, Tree-sitter 0.25.2 pinned to avoid reproduced 0.26.0 native corruption). Python-only results unchanged; no JS dead-export/Markdown/mutation claim.
- Overlaps skip strict same-file JS/TS ancestor/descendant source-span pairs and compare within one language pool; siblings, cross-file pairs and embedding inputs are kept.

### Fixed
- `overlaps`: any exception while loading/running the pass-2 model (e.g. transformers 5.x `all_tied_weights_keys`) warns once, naming the cause, and falls back to pass 1. A JS/TS file that does not parse is skipped with a warning instead of aborting the run; bare `&` in JSX text (rejected by both grammars) now parses. Python methods and their nested closures are no longer reported as overlaps.

### Changed
- Isolate every native pytest pass/fork in a private cleaned scratch tree. Bound mutation tests with configurable pytest-timeout safety (default 5s, max 60s); keep harness-health counters strict and preserve explicit native budgets.
- Skip comment-only mutation work only after strict merge-base AST equivalence; retain added files, defaults, decorators, annotations and all real code changes, with an explicit no-op receipt.
- Preserve IVAI's security command and dynamic-import authorization checks alongside standalone assertion and fail-fast rules.
- Use native mutmut 3.7 configuration while retaining failed-baseline rejection, test-pass isolation, source/process identity, macOS proxy safety and tracked-child reaping.
- Port updated CLI/native-owner regression contracts; keep repository mutation assets and roots configurable. Repository import roots no longer evict virtual-environment dependencies.
- Permit Transformers 5.x so consuming repositories can retain their patched dependency constraints.
- Remove an overwritten partial detector definition that broke the package's native lint gate; add a duplicate-definition regression contract.
- `deadcode overlaps` is now two-pass by default: pass 1 deterministic struct+API (unchanged), pass 2 CodeSage-small-v2 (pinned revision, embed-only) adds type-4 candidates as `embed_pairs`. Falls back to pass 1 with one warning when the model or extras are missing; `--no-embed` skips pass 2.

### Removed
- `--embed` and `--embed-topk` (the hybrid and two-stage scoring did not pass the q162 rule).
