# Changelog

## [Unreleased]

### Added
- Added `reachability/duplicate-constant` (warning): the same module-level string constant (≥10 chars, not a dotted import path or dunder, not in `__all__`) assigned in 2+ runtime modules is reported, naming the module the others import (transitively) as its owner.
- Added advisory rule `code-quality/query-with-effects` (complexity detector): query-named functions (get_/is_/has_/can_/find_/read_/check_/list_/load_) that write (INSERT/UPDATE/DELETE/REPLACE execute, commit, file write/unlink, HTTP post/put/delete/patch, record_/save_/persist_/append_/insert_/write_*_atomic/atomic_write/write_json*/dump_json calls or callbacks) are flagged; get_or_*/*_or_compute*/ensure_* exempt.
- `mutation` extra pins `pytest>=8,<9.2` (mutmut 3.7.0 breaks with pytest 9.2: `Function` has no `_nodeid`).
- `run-mutmut-changed --only-touched-functions` (default off): mutate only functions whose lines the diff touched, so the kill rate covers those mutants only. Module/class-level-only changes mutate nothing (reported on stderr); added files and dirty files mutate whole.
- `stub-sensitivity`: advisory audit stubbing each function body (`return None`/`raise`) in a scratch copy; lists functions no covering test detects. Red baseline exits 2; target tree untouched. Finds functions no covering test notices crashing/failing, not weak assertions; generators skipped; import-time-only functions are 'not covered'. Per-stub RSS cap (max 2 GiB, 4x baseline) => detected_by 'memory'; BOM sources accepted and restored byte-exact.
- `overlaps`: function embeddings persist in SQLite under `$XDG_CACHE_HOME/deadcode-audit` (vectors only, keyed by source hash and pinned model/tokenizer/library identity); unchanged functions are not re-encoded. Unpinnable models or cache failures compute normally.
- `growth --base BASE --head HEAD`: size and churn of changed non-test Python files, ported from agent-skills. JSON includes counts, deltas, exclusions and AST-unknown locators. No model, checkout, score change or new blocking threshold.
- Mixed Python/JS/TS scan/CI and function overlaps with exact source identities (`javascript` extra; Oxlint 1.90.0, Tree-sitter 0.25.2 pinned to avoid reproduced 0.26.0 native corruption). Python-only results unchanged; no JS dead-export/Markdown/mutation claim.
- Overlaps skip strict same-file JS/TS ancestor/descendant source-span pairs and compare within one language pool; siblings, cross-file pairs and embedding inputs are kept.

### Fixed
- `ai-slop/redundant-try-catch`: a bare `raise` handler followed by a later handler that could catch the same error (bare/`Exception`/builtin supertype, or unresolvable) shields it and is no longer reported.
- `reachability-scan`/`scan` honor `project.vulture_whitelist` (names in it are consumed, as for Vulture). Vulture ignores pydantic `model_config`/`model_fields` by default.
- `hardcoded-secret`: a `*_token`-named constant holding a lowercase kebab/dotted label of 2-5 letter-only words (e.g. `"triage-approved"`) is no longer flagged; strong name parts (password/secret/api_key/...) and known credential shapes are unaffected.
- `overlaps`: any exception while loading/running the pass-2 model (e.g. transformers 5.x `all_tied_weights_keys`) warns once, naming the cause, and falls back to pass 1. A JS/TS file that does not parse is skipped with a warning instead of aborting the run; bare `&` in JSX text (rejected by both grammars) now parses. Python methods and their nested closures are no longer reported as overlaps.

### Changed
- `cycles`: `if TYPE_CHECKING:` imports are no longer edges. Cycles that exist only through a function-local import are SOFT (listed in their own section/`kind: soft`, never block); only HARD cycles exit 1. JSON gains `kind`. `--strict-lazy` restores lazy edges as hard.
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
