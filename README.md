# deadcode-audit

Quality, dead-code, import-cycle, duplication and mutation checks for a target repository, for Python and (optionally) JavaScript/TypeScript.
The target repository supplies paths and policy, not scanner code.

## Start

Requires Python 3.11+ and Git.

```sh
# In this checkout:
uv sync --extra vulture
# In the repository to audit:
/path/to/deadcode-audit/.venv/bin/deadcode scan
```

The target is the current directory, or `DEADCODE_REPO_ROOT`. `python -m deadcode_audit` is the same entry point; Python API: `from deadcode_audit import config, scan`.
Alternative install: `uv pip install 'deadcode-audit @ git+https://github.com/Vlislavn/deadcode-audit.git@<commit>'` — pin a commit; private repositories need GitHub access, and no credentials belong in configuration.

## Configure

Without configuration, existing `src/` and `scripts/` directories are scanned; otherwise the current tree. Tests, virtual environments, Git metadata, node_modules and mutation workspaces are always excluded.
For nested layouts, commit `.deadcode.yml` (paths relative to the target; missing roots and malformed configuration fail visibly):

```yaml
project:
  source_roots: [tools, plans/scripts]
  import_roots: [tools/production, '.']
  test_roots: [tests, tools/tests]
  mutation_roots: [tools/production]
  mutation_also_copy: [tests]
  mutation_test_map:
    tools/production: [tests]
```

`import_roots` identifies Python import bases; configure each separate application if necessary.
Rules, scoring, exclusions, accepted cycles and CI thresholds go in the same file; `deadcode rules` lists rule IDs. Optional `vulture_whitelist` is a list of target-owned files. See `examples/monorepo/` for the migration policy.

## Commands

| Goal | Command |
|---|---|
| Findings and score | `deadcode scan [paths...] [--json]` — Python plus JS/TS (`javascript` extra + Oxlint) |
| Enforce quality policy | `deadcode ci --json` — exit 1 on errors or score below threshold |
| Security and swallowed errors | `deadcode security [paths...]` — fail-closed, independent of configurable rule severity |
| Unused public symbols | `deadcode reachability-scan --json` — advisory |
| Import cycles | `deadcode cycles [--advisory] --json` |
| Size and churn | `deadcode growth --base BASE [--head HEAD] [--json] [files...]` — changed non-test Python files, exact commits; no bloat threshold |
| Function overlap | `deadcode overlaps [paths...] --json` — advisory, two-pass; Python and JS/TS (`--no-embed` for pass 1 only) |
| Diff gates vs main | `deadcode vulture-changed`, `redundancy`, `runtime-consumer-check` — all with `--compare-branch main` |
| Mutation gate | `deadcode mutation-targets`, `deadcode run-mutmut-changed` — `--compare-branch main` |
| Helpers | `deadcode rules`, `trend`, `changed-python-files`, `mypy-targets` — `--null` for machine output |

Advisory exit 0 means the audit ran, not that the code is clean; parse errors and command failures are not a clean result.
`growth` reports physical lines (including blanks/comments), functions (including methods/nested/async), classes, import instructions and short function bodies (≤5 lines after an initial docstring). Net change and churn are separate. Syntax errors make AST counts `null`/unknown with file locators, never zero; excluded files and binary churn are explicit. It does not execute source, load a model or alter history. It measures size, not necessity.

Other diff checks compare committed HEAD against the merge base; whole-tree scans include untracked supported source (Python-specific checks still select Python only). Mutation selection skips only strict AST-equivalent changes (such as comments), reports that no-op explicitly, and still selects added or behaviorally changed files.

## JavaScript / TypeScript

Optional; Python-only behaviour is unchanged. `scan`/`ci` accept `.js .cjs .mjs .jsx .ts .cts .mts .tsx` next to Python and run a pinned local **Oxlint 1.90.0** (only that version; Node `^20.19.0` or `>=22.12.0`). Install it once in the **target** checkout:

```sh
npm install --prefix .git/deadcode-js --ignore-scripts --no-audit --no-fund --save-exact oxlint@1.90.0
export DEADCODE_OXLINT="$PWD/.git/deadcode-js/node_modules/.bin/oxlint"
/path/to/deadcode-audit/.venv/bin/deadcode scan src scripts --json --no-history
```

- Scans never install tools, run `npx`, fetch rules, execute sources, load target plugins/configs or auto-fix. A missing/other backend version, malformed reply, incomplete coverage or process failure refuses.
- Roots come from `project.source_roots` (e.g. `[src, javascript]`). Reported: unused local bindings/empty blocks (warnings), unreachable statements and syntax errors (errors). Host-invoked CommonJS exports are not declared dead. Markdown fences, unused-export graphs, reachability, Vulture and mutation stay Python-only.
- JS-containing JSON adds `coverage: {python: N, javascript_typescript: N}`. Scoring uses the existing aggregate formula; it is not a calibrated JS defect rate.
- Batches of ≤128 files, two threads, `DEADCODE_JS_SAFETY_SECONDS` wall-clock cap (default 60, max 120 per batch), replies >8 MiB rejected. `DEADCODE_OXLINT` is one executable, not shell arguments. Real-backend tests: `DEADCODE_TEST_OXLINT="$DEADCODE_OXLINT" uv run --no-sync pytest -q`.
- **Overlaps:** with `--extra javascript` (plus `embeddings` for pass 2), `deadcode overlaps src javascript --json --top 0` extracts functions with official local Tree-sitter grammars (`tree-sitter==0.25.2`; 0.26.0 corrupts memory and is rejected). Both passes compare within a language pool (JS and TS share one; Python↔JS is never compared); strict same-file ancestor/descendant spans (Python too: a method and its nested closure) are skipped. JS-containing JSON adds per-function language, source hash/bytes/byte span, eligibility and embedding status. A file that does not parse is skipped with one `warning:` naming it; the run continues. Bare `&` in JSX text (valid JSX the grammars reject) is parsed, see [docs/JAVASCRIPT.md](docs/JAVASCRIPT.md#parser-compatibility).

Backend contract, primary sources, parser evidence: [docs/JAVASCRIPT.md](docs/JAVASCRIPT.md).

## Extras

```sh
uv sync --all-extras   # or pick: --extra vulture --extra mutation --extra embeddings --extra javascript
uv run --no-sync pytest -q
uv build
```

- **Two-pass overlaps** (`overlaps`, default): pass 1 is deterministic (structural tokens + API-name Jaccard, `--threshold`, keys `pairs`/`count`); pass 2 embeds each function with `codesage/codesage-small-v2` (pinned revision, embed-only, masked mean-pool, L2, cosine) and lists each function's top-3 neighbours missing from pass 1 as type-4 candidates (keys `embed_pairs`/`embed_count`). `--no-embed` runs pass 1 only; if loading or running the model fails for any reason (missing torch/transformers, no model, an exception inside transformers) the command prints one `warning:` line naming the cause to stderr and returns pass 1 (`embed_active: false`). The pinned CodeSage remote code currently needs transformers <5: on 5.x (`'CodeSageModel' object has no attribute 'all_tied_weights_keys'`) pass 2 falls back to pass 1 with that warning. The first run downloads CodeSage (~1 GB) and executes its pinned repository code (`trust_remote_code`, pinned ids only); inference is local CPU, no paid API; `--top N` applies per pass (`0` = all). On 50 real helpers with hand-written type-4 rewrites CodeSage embed-only reached recall@3 0.92 vs 0.63 for pass 1; candidates are leads, not proof of equivalence or permission to delete code. Memory grows quadratically with eligible function count — supervise large runs.
- **Mutation** (`mutmut` 3.7.0): POSIX only — use macOS/Linux/WSL. The native owner copies broad roots but mutates only the selected files. Failed baselines stop the run; repeated test passes, subprocess associations and process identity remain isolated. Repository-read assets belong in `project.mutation_also_copy`. Runs use a disposable `mutants/` directory, plus a private cleaned scratch tree for every pytest pass/fork. The mutation-only per-test safety default is 5s; `DEADCODE_MUTMUT_TEST_TIMEOUT_SECONDS` can override it within `(0, 60]`. Explicit `[tool.mutmut].pytest_add_cli_args` timeout settings are preserved unless the environment overrides them; broad global suite addopts are not the mutation budget.
- Install extras together: a subsequent exact sync with fewer extras removes previously installed ones. The Vulture integration test requires its extra.

Upstream notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
License: [MIT](LICENSE). Changes: [CHANGELOG.md](CHANGELOG.md).
