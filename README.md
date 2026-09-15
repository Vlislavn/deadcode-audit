# deadcode-audit

One Python package for quality, dead-code, import-cycle, duplication and mutation checks.
Extracted from the source monorepo; the target repository supplies paths and policy, not scanner code.

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
Optional `vulture_whitelist` points to a target-owned whitelist file.
Rules, scoring, exclusions, accepted cycles and CI thresholds are configured in the same file;
`deadcode rules` lists available rule IDs. See `examples/monorepo/` for the migration policy.

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
Whole-tree scans include untracked Python source. Advisory exit 0 means the audit ran, not that the code is clean.
Parse errors, unavailable models and command failures are not a clean result.

## Embeddings

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

CI runs core/CLI tests independently of the source monorepo. The Vulture integration test requires its extra.
Model inference is an explicit optional integration run, not a network dependency in every test.
See [PROVENANCE.md](PROVENANCE.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for source attribution.
