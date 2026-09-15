# deadcode-audit

Quality, dead-code, import-cycle, duplication and mutation checks for a target repository.
Extracted from the source monorepo; the target repository supplies paths and policy, not scanner code.

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
| Findings and score | `deadcode scan [--json]` |
| Enforce quality policy | `deadcode ci --json` — exit 1 on errors or score below threshold |
| Unused public symbols | `deadcode reachability-scan --json` — advisory |
| Import cycles | `deadcode cycles [--advisory] --json` |
| Function overlap | `deadcode overlaps --json` — advisory |
| Diff gates vs main | `deadcode vulture-changed`, `redundancy`, `runtime-consumer-check` — all with `--compare-branch main` |
| Mutation gate | `deadcode mutation-targets`, `deadcode run-mutmut-changed` — `--compare-branch main` |
| Helpers | `deadcode rules`, `trend`, `changed-python-files`, `mypy-targets` — `--null` for machine output |

Advisory exit 0 means the audit ran, not that the code is clean; parse errors and command failures are not a clean result.
Diff checks compare committed HEAD against the merge base; whole-tree scans include untracked Python source.

## Extras

```sh
uv sync --all-extras   # or pick: --extra vulture --extra mutation --extra embeddings
uv run --no-sync pytest -q
uv build
```

- **Embeddings** (`overlaps`): the first run downloads CodeBERT; inference is local CPU, no paid API. `--no-embed` uses the deterministic path only. Model or dependency failures stop the command. Cosine similarity is a heuristic for inspection, not permission to delete code. Memory grows with eligible function count — supervise large runs.
- **Mutation** (`mutmut`): POSIX only — use Linux/WSL. Runs execute repository tests in a disposable `mutants/` directory.
- Install extras together: a subsequent exact sync with fewer extras removes previously installed ones. The Vulture integration test requires its extra.

Upstream notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
License: [MIT](LICENSE).
