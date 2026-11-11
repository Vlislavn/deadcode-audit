# deadcode-audit

One Python package for Python dead-code, import-cycle, duplication and mutation checks,
plus JavaScript/TypeScript quality through an optional pinned local Oxlint backend.
The same `scan`/`ci` commands combine both languages. Function `overlaps` shares the existing
embedding pipeline across Python and JS/TS; JS export reachability and mutation are not implemented. Extracted from IVAI; the target supplies paths and policy, not scanner code.

[Changelog](CHANGELOG.md).

## Start here

Python 3.11+ and Git are required. Clone this repository, then:

```sh
uv sync --extra vulture
cd /path/to/your/repository
/path/to/deadcode-audit/.venv/bin/deadcode scan
```

For an installed version, use `uv pip install 'deadcode-audit @ git+https://github.com/Vlislavn/deadcode-audit.git@<commit>'`.
Pin a commit in consuming projects. Private repositories require GitHub access; no credentials belong in configuration.
`python -m deadcode_audit` is the same entry point. Python integrations use `from deadcode_audit import config, scan`.
The target is the current directory, or the explicit `DEADCODE_REPO_ROOT` environment variable.

## Configure your repository

Without configuration, existing `src/` and `scripts/` directories are scanned; otherwise the current tree is used.
Tests, virtual environments, Git metadata, node_modules and mutation workspaces are excluded from runtime scans.
For nested layouts, commit `.deadcode.yml`:

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

Paths are relative to the target repository. Missing roots and malformed configuration fail visibly.
`import_roots` identifies Python import bases; configure each separate application if necessary.
Optional `vulture_whitelist` is a list of target-owned files, e.g. `[scripts/vulture_whitelist.py]`.
Rules, scoring, exclusions, accepted cycles and CI thresholds are configured in the same file;
`deadcode rules` lists available rule IDs. See `examples/ivai/` for the migration policy.

## Choose a check

| Purpose | Command | Outcome |
|---|---|---|
| Whole-tree quality findings | `deadcode scan --json` | Advisory findings and score |
| Enforce quality policy | `deadcode ci --json` | Nonzero on errors or score below threshold |
| Possible unused public symbols | `deadcode reachability-scan --json` | Advisory; dynamic consumers need review |
| Import cycles | `deadcode cycles --advisory --json` | Advisory; omit `--advisory` to enforce |
| Semantic function overlap | `deadcode overlaps --json` | Advisory; real embeddings required |
| Deterministic overlap only | `deadcode overlaps --no-embed --json` | Explicitly disables embeddings |
| Available rules / history | `deadcode rules` / `deadcode trend` | History is local to the target |
| Changed files / type-check targets | `deadcode changed-python-files --compare-branch main` / `deadcode mypy-targets --compare-branch main` | Paths; `--null` for machine consumers |
| Changed-line Vulture / redundancy | `deadcode vulture-changed --compare-branch main` / `deadcode redundancy --compare-branch main` | Gates plus advisory candidates |
| New symbols without consumers | `deadcode runtime-consumer-check --compare-branch main` | Conservative static gate |
| Mutation targets / execution | `deadcode mutation-targets --compare-branch main` / `deadcode run-mutmut-changed --compare-branch main` | Changed-code mutation gate |

Diff checks compare committed HEAD against the merge base. They do not include unstaged work.
Whole-tree quality scans include untracked supported source; Python-specific checks still select Python only. Advisory exit 0 means the audit ran, not that the code is clean.
Parse errors, unavailable models and command failures are not a clean result.

## JavaScript / TypeScript alongside Python

Install the optional backend once in the **target** checkout (Node `^20.19.0` or `>=22.12.0`):

```sh
npm install --prefix .git/deadcode-js --ignore-scripts --no-audit --no-fund --save-exact oxlint@1.90.0
export DEADCODE_OXLINT="$PWD/.git/deadcode-js/node_modules/.bin/oxlint"
/path/to/deadcode-audit/.venv/bin/deadcode scan src scripts --json --no-history
```

Alternatively put that exact version of `oxlint` on PATH. Scans never install tools, invoke `npx`,
fetch rules, execute input sources, load target JS plugins/configs, or auto-fix files.
Missing/other backend versions, malformed replies, incomplete coverage and process failures refuse.
The adapter accepts **1.90.0 only**: the upstream JSON protocol is not versioned.

Use the existing `project.source_roots` for a default mixed scan; no second root configuration:

```yaml
project:
  source_roots: [src, javascript]
  test_roots: [tests]
```

Supported files: `.js`, `.cjs`, `.mjs`, `.jsx`, `.ts`, `.cts`, `.mts`, `.tsx` and Python.
The JS quality slice reports unused local bindings/empty blocks as warnings, unreachable statements
and syntax errors as errors. Host-invoked CommonJS exports are **not** declared dead. Markdown
fences, unused-export graphs and JS mutation are outside this slice. Reachability, Vulture and
mutation commands remain Python-only; the function-overlap audit below supports both languages.

JS-containing JSON adds `coverage: {python: N, javascript_typescript: N}`; Python-only JSON,
scores and `run_scan` behavior stay unchanged. Mixed scoring uses the existing aggregate formula,
not a calibrated JS defect/removability percentage. Findings may need context and can overlap;
never interpret a low score as proof that callbacks or fallback handling should be deleted.

The backend is run on explicit batches of at most128files with two threads. The wall-clock
safety cap is `DEADCODE_JS_SAFETY_SECONDS` (default60, maximum120seconds per batch);
missing/invalid budgets refuse. Responses above8MiB are rejected after capture—this is not
an OS allocation sandbox. `DEADCODE_OXLINT` is a single executable, not shell arguments.
Run real optional backend tests with `DEADCODE_TEST_OXLINT="$DEADCODE_OXLINT" uv run --no-sync pytest -q`.
See [backend contract and primary-source decisions](docs/JAVASCRIPT.md).

## Embeddings

```text
Python AST --------------------+
JS/TS Tree-sitter functions ----+-> existing CodeBERT -> same overlap scoring/report
                                  Python and JS/TS are separate comparison pools
```

For JS/TS function embeddings, install the parser and embedding extras **together** (no Oxlint required):

```sh
uv sync --all-extras
# From the target checkout; paths are optional, like scan:
/path/to/deadcode-audit/.venv/bin/deadcode overlaps src javascript --json --top 0
```

The frontend imports official local Tree-sitter grammars, not a regex function extractor. It handles
declarations, async/generator functions, arrows, function expressions, class/object methods, nested
callbacks and CommonJS assignments. Whole-file syntax errors refuse analysis before model loading;
source is never executed. The parser is pinned to `tree-sitter==0.25.2`: published0.26.0 has an
upstream `Point.row` reference-count corruption defect that reproduced as SIGBUS here. A runtime
version check rejects that unsafe override; the actual same-corpus counterfactual and upstream fix
are recorded in [backend evidence](docs/JAVASCRIPT.md#parser-compatibility).

The same encoder, `--model`, weights, `--min-tokens`, `--threshold`, `--top` and `--no-embed` options
work for JS/TS. JavaScript and TypeScript compare together; Python↔JS structural/API scores are not
calibrated, so cross-language pairs are intentionally not emitted. One report can include both pools.
Python-only report shape/scores remain unchanged. JS-containing JSON additionally identifies each
function's language, exact source hash/bytes/byte span, eligibility and embedding status, with per-language
counts. Two same-line anonymous functions retain distinct identities.

Historical pre-containment-filter CPU CodeBERT verification: 49 JS files yielded 425 functions;
314 met default `--min-tokens 40` and were actually embedded, producing 1743 candidate pairs before
the same-file ancestor/descendant filter. A mixed run embedded 487 functions (173 Python + 314 JS/TS).
The original Python report (173 embeddings / 292 pairs) remained byte-identical. No post-filter
real-data pair count is asserted here. These are execution/coverage numbers, not precision, semantic
equivalence or removal permission.
The111small JS functions are explicitly ineligible at the default threshold; use `--min-tokens 1`
when intentionally including small functions.

```sh
# In this checkout; installs the model runtime, not model weights.
uv sync --extra embeddings --extra vulture
# From the target checkout:
/path/to/deadcode-audit/.venv/bin/deadcode overlaps --model microsoft/codebert-base --json
```

The first run downloads CodeBERT from Hugging Face. Computation is local CPU inference,
with two CPU threads, batches of four, and at most 512 tokens per function.
There are no paid LLM API calls. Model/dependency failures stop the command; no silent deterministic fallback.
The JSON records model revision, functions scanned/embedded, pooling, truncation limit and cosine scores. Scan JSON separately records `files_scanned`; `summary.files` counts files with findings.
`--min-tokens` filters small functions; `--top 0` prints all matching pairs.
Cosine similarity is a heuristic for inspection, not proof of semantic equivalence or permission to delete code.
The comparison combines cosine, normalized syntax and API-name overlap; the API filter can miss semantic matches.
JS local/parameter bindings are abstracted while free APIs, property names and operators are retained.
Identical snippets and injected-vector tests are trivial wiring floors, not evidence of real-data accuracy.
Memory grows quadratically with eligible function count. Supervise large runs and set process limits on your host.

## Mutation and validation

Install `uv sync --extra mutation` for the pinned mutmut 3.4.0 adapter.
Mutation runs execute repository tests and use a disposable `mutants/` directory; do not keep personal files there.
Native Windows mutation is unsupported: use Linux/WSL. Scans and embeddings support ordinary Python environments.
Neither CPython parsing nor static analysis verifies Revit/IronPython runtime behavior.

```sh
uv sync --all-extras
uv run --no-sync pytest -q
uv build
```

The CI workflow declares core/CLI checks independently of IVAI; its recorded startup status is below. The Vulture integration test requires its extra.
Model inference is an explicit optional integration run, not a network dependency in every test.
See [PROVENANCE.md](PROVENANCE.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for source attribution.

## Migration and verification — 2026-09-15

IVAI's `scripts/deadcode/` and 17 dedicated test files were removed from the migration branch. Implementation and those tests now live here; clients import `deadcode_audit` and pin its Git revision. There is no compatibility shim at the old module path. IVAI retains `.deadcode.yml` policy and consumer tests; KL's dispatcher uses its installed package, with `DEADCODE_PYTHON` as the interpreter override instead of `IVAI_REPO`.

Validation at `8195c94`: **450 local tests passed**, including real CLI mutation checks (a killed mutant passes; a failing clean baseline is rejected). IVAI: five consumer tests passed, including invocation from another working directory. A built wheel was installed outside IVAI and its CLI executed. Consumers currently pin implementation commit `a95aef4`; later commit `8195c94` adds tests only.

Real CodeBERT inference was exercised on SKAN's configured runtime corpus: 275 files, 2496 functions, 1940 embedded; 142 seconds and approximately 1.1 GiB sampled peak RSS. These measurements describe that input snapshot, not arbitrary repositories or a full scan of every SKAN file. Model revision: `3b0952feddeffad0063f274080e3c23d75e7eb39`.

The repository is private. The recorded [GitHub run](https://github.com/Vlislavn/deadcode-audit/actions/runs/34940212252) ended with `startup_failure` before jobs; no CI pass is claimed and the API did not expose the cause. No billing/quota settings were changed. Mutation tests require POSIX; no Windows mutation support is claimed.

When combining features, install the extras together (`uv sync --extra vulture --extra mutation --extra embeddings`); a subsequent exact sync with fewer extras can remove optional dependencies. CodeBERT works with `trust_remote_code=False`; arbitrary Hugging Face models are not guaranteed compatible with this tokenizer/AutoModel adapter.
