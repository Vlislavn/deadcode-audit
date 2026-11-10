# deadcode-audit

One Python package for quality, dead-code, import-cycle, duplication and mutation checks.
Extracted from IVAI; the target repository supplies paths and policy, not scanner code.

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
| Function overlap (two-pass) | `deadcode overlaps --json` | Advisory; pass 1 deterministic, pass 2 embedding candidates (falls back to pass 1 with a warning) |
| Deterministic overlap only | `deadcode overlaps --no-embed --json` | Pass 1 only; never loads a model |
| Available rules / history | `deadcode rules` / `deadcode trend` | History is local to the target |
| Changed files / type-check targets | `deadcode changed-python-files --compare-branch main` / `deadcode mypy-targets --compare-branch main` | Paths; `--null` for machine consumers |
| Changed-line Vulture / redundancy | `deadcode vulture-changed --compare-branch main` / `deadcode redundancy --compare-branch main` | Gates plus advisory candidates |
| New symbols without consumers | `deadcode runtime-consumer-check --compare-branch main` | Conservative static gate |
| Mutation targets / execution | `deadcode mutation-targets --compare-branch main` / `deadcode run-mutmut-changed --compare-branch main` | Changed-code mutation gate |

Diff checks compare committed HEAD against the merge base. They do not include unstaged work.
Whole-tree scans include untracked Python source. Advisory exit 0 means the audit ran, not that the code is clean.
Parse errors, unavailable models and command failures are not a clean result.

## Embeddings

`deadcode overlaps` runs two passes:

1. **Pass 1, deterministic (always):** all function pairs scored by structural token similarity + API-name Jaccard (`--threshold`, default 0.55). Output keys `pairs`/`count`.
2. **Pass 2, embedding (default):** `codesage/codesage-small-v2`, pinned to revision `4844c2f24b25e181aa43ca058cc73dd2622565c1`, embed-only (masked mean-pool, L2, cosine). Each function's top-3 cosine neighbours not already in pass 1 are listed as type-4 candidates (same behaviour, different code), ranked by cosine. Output keys `embed_pairs`/`embed_count`, with `embed` = cosine and the pass-1 sub-scores for context.

`--no-embed` runs pass 1 only. If torch/transformers or the model are not available, the command prints one `warning:` line to stderr and returns pass 1 only (`embed_active: false`).
Evidence: on 50 real helpers with hand-written type-4 rewrites, CodeSage embed-only reached recall@3 0.92 vs 0.63 for pass 1 and passed the pre-registered rule. It is one split of short pure helpers; treat candidates as leads, not proof of equivalence or permission to delete code.

```sh
# In this checkout; installs the model runtime, not model weights.
uv sync --extra embeddings --extra vulture
# From the target checkout:
/path/to/deadcode-audit/.venv/bin/deadcode overlaps --json
```

The first default run downloads CodeSage (about 1 GB) from Hugging Face and executes its repository code (`trust_remote_code`, allowed only for pinned ids; a small shim keeps it working with transformers >= 4.50). Computation is local CPU inference,
with two CPU threads, batches of four, and at most 512 tokens per function. There are no paid LLM API calls.
`--model` selects another model (codesage-small-v2 and jina-v2-base-code use pinned remote code; other ids are not guaranteed compatible). The JSON records model revision, functions scanned/embedded, pooling and truncation limit. Scan JSON separately records `files_scanned`; `summary.files` counts files with findings.
`--min-tokens` filters small functions; `--top N` applies to each pass separately (`0` = all).
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
