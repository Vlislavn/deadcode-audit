# Optional JavaScript / TypeScript quality backend

```text
existing scan/ci discovery -> Python -> existing AST detectors --+
                          -> JS/TS -> local Oxlint1.90.0 --------+-> Diagnostic -> overrides/score/report/CI
Python AST functions ----------+-> two-pass overlaps (deterministic + CodeSage embed-only), language-scoped pools
JS/TS AST functions -----------+
Python graph/mutation ------------------------------------------ unchanged, Python-only
```

## Why reuse Oxlint

The existing scanner discovers `.py` and builds `ast.Module`; changing globs alone cannot parse JS.
Reuse is narrower than porting every detector or adding another scanner CLI: an adapter invokes the
published local backend and maps its diagnostics to the existing schema. There is no new model, custom
JS parser, generic plugin framework, auto-writer or runtime dispatcher. Function extraction imports
official local Tree-sitter grammars and feeds the existing embedding pipeline.

Pinned primary source: [Oxlint1.90.0](https://github.com/oxc-project/oxc/releases/tag/oxlint_v1.90.0),
commit `9b6fff42fa6edf0bec55861b6f0764c7b9acd86b`, MIT. Its
[JSON formatter](https://github.com/oxc-project/oxc/blob/9b6fff42fa6edf0bec55861b6f0764c7b9acd86b/apps/oxlint/src/output_formatter/json.rs#L72-L91)
provides diagnostics and actual file counts; the schema itself has no version field.
Source/license, published npm metadata and actual CLI output were checked before implementation.

| Considered | Decision |
|---|---|
| Oxlint1.90.0 | Reuse ready local JS/TS syntax and three quality rules; strict version/transport contract. |
| Knip6.43.0, ISC | Useful later for export graphs, but host entrypoints/dynamic consumers must be modeled; not a syntax-quality backend. |
| Aislop0.18.1, MIT | Closest diagnostic shape, but broader UI/network/dependency scope; its Python launcher uses `npx --yes`, and its Oxlint adapter can return empty findings on invalid output. Not imported wholesale. |
| jscpd5.4.2, MIT | Useful for clone candidates, not unused/parse analysis; tokenizer fallback after fatal parsing is not reliable AST coverage. No clone capability claimed here. |
| Biome2.6.3, MIT/Apache-2.0 | Capable alternative, but its JSON reporter is experimental. Not added alongside another backend for this slice. |

A bounded GitHub sweep used multiple query families (unused/dead code, multi-language analyzers,
Python adapters, Oxlint/Biome/Aislop/Skylos, JS clone adapters), including updated/created ordering.
Search hits were distinguished from reviewed source; public API rate limits were not claimed as
successful searches. No ready narrow Python adapter satisfying the fail-closed contract was found;
that does not prove none exists. Recent bridge-analysis evidence
[arXiv:2610.27288v1](https://arxiv.org/abs/2610.27288v1), CC-BY4.0, concerns Android/WebView—not Obsidian
or the effectiveness of this adapter. It supports keeping unresolved dynamic edges explicit, not
claiming their absence means dead code.

## Boundary contract

- Source files are inputs, not programs to execute. The adapter supplies its own JSON config, disables
  nested target configs, selects exactly three built-in rules, disables ignore-file filtering and
  passes explicit batches. No npm/npx/network operation occurs during scanning.
- `javascript/unused-variable` and `javascript/empty-block` are warnings; `javascript/unreachable`
  and `javascript/parse-error` are errors. Existing per-rule severity overrides apply. `scan` remains
  advisory; `ci` gates error findings and the configured score as before.
- Missing executable, unsupported version, timeout, unexpected stderr, malformed or duplicated JSON,
  unknown rule/severity/path/location, coverage mismatch, contradictory exit status or source drift
  refuses the run. Findings are buffered until every batch completes. No partial successful report.
- Locations are validated against captured input bytes; the before/after input check is not atomic
  writer exclusion or a general sandbox. Files can still be externally modified after the last check.
- CommonJS exports, QuickAdd `command.path`/named members, Templater functions and embedded Markdown
  contexts are not a complete static call graph. This quality slice does not recommend their deletion.
  Do not interpret all unused catch bindings, callback parameters or empty fallback blocks as defects.

## Verification and limits

The pre-change canonical suite had450passing tests. A JS-only scan failed with no Python sources;
that negative was captured before the CLI acceptance test and implementation. Tests include actual
local backend JS/CJS/TS/TSX, mixed source, exports/host globals, source/config side-effect sentinels,
syntax errors, exclusions, committed `--changed`, coverage/transport negatives and unchanged Python
API/output. Protocol-unit fixtures are wiring floors, not semantic analysis or native host proof.

The first real source-only run examined49Obsidian JS files, not a personal vault:61warnings,
0errors (39unused-binding findings and22empty blocks), aggregate score23. Those are rule candidates,
not61confirmed defects or a calibrated quality number. No source was fixed to improve the score.
Python-only static reports before/after are byte-identical on the same16-file source cohort.
Final validation records exact source/command receipts in the local integration artifacts. Linux CI is configured to install the pinned backend;
no remote CI execution or Windows-backend support is asserted without its own receipt.

## Shared function embeddings

Optional `javascript` and `embeddings` extras add AST function extraction to the existing `overlaps`
command, not a second analyzer UI. `overlaps src javascript --json --top 0` accepts the same explicit
source targets as `scan`; omitted paths use configured roots. Explicit targets honor configured
excludes for both languages. Default JS discovery honors excludes; default Python overlap discovery
retains its historical root-only selection (excludes are not newly imposed on Python). The grammars are official
[tree-sitter-javascript0.25.0](https://github.com/tree-sitter/tree-sitter-javascript/tree/v0.25.0) and
[tree-sitter-typescript0.23.2](https://github.com/tree-sitter/tree-sitter-typescript/tree/v0.23.2), MIT.
The Python binding is [py-tree-sitter0.25.2](https://github.com/tree-sitter/py-tree-sitter/tree/v0.25.2), MIT.
No regex/keyword test grades behavioral meaning. Parsed runtime bodies are structurally normalized;
API/property names and operators are retained, locals are scope-normalized, TS type-only syntax is
omitted. Fingerprints and embedding proximity remain candidates, never functional-equivalence proof.

Each source slice comes from the same captured UTF-8 buffer as the AST. Byte offsets, human location,
language, source hash/size, eligibility and embedding status identify inputs and pair endpoints.
Anonymous callbacks and nested functions on one line cannot collapse to one identity. Every file is
validated before the model is loaded; malformed late input cannot yield a partial successful result.
Known dynamic entrypoints are not treated as dead. Python and JS/TS share the same two-pass pipeline
(pass 1 deterministic struct+API; pass 2 pinned CodeSage embed-only candidates, with its pooling,
normalization, batch size and `--model` option), but compare only within language pools in both passes; JS and TS share one pool. Strict same-file JS/TS ancestor/descendant byte-span pairs
are removed before scoring because nested function bodies are not independent duplicates; identical
siblings and cross-file pairs remain candidates. JS-containing JSON reports expose
`pairs_skipped_same_file_containment` for functions eligible at `min_tokens`; the Python-only report
shape is unchanged. Cross-language
structural/API scores are not calibrated.

Historical receipts (hybrid CodeBERT scoring, before the two-pass default and before the containment filter): original Python192functions/173eligible
embeddings/292pairs; the final Python JSON is byte-identical. The49-JS-file cohort contains
425functions,314eligible at default40tokens,314actual CodeBERT embeddings and1743pairs above the
unchanged threshold. Mixed input contains617functions and embeds487 (173Python+314JS), with2035
pairs and no cross-language pairs. These historical candidate counts predate the same-file
ancestor/descendant filter; no post-filter real-data pair count is asserted here. The model revision
is `3b0952feddeffad0063f274080e3c23d75e7eb39`; no fallback or replacement model was used. These are
real execution numbers, not a precision/recall benchmark. Identical loader snippets and perfect
cosine in synthetic fixtures are wiring floors; candidate pairs still need semantic review.
Source files, live vault/settings, backend clients and the consumer's Git-pinned scanner are unchanged.
The new canonical CLI was exercised directly; a consumer release pin is not fabricated.

<a id="parser-compatibility"></a>
## Parser compatibility

The first actual JS+embedding run (CodeBERT at the time) exposed a native SIGBUS before inference. Module/parser unit tests
alone did not catch it. CPython's GC crashed during unrelated torch/transformers imports after the
frontend read many `Point.row` values. In published0.26.0,
[point.c:35–40](https://github.com/tree-sitter/py-tree-sitter/blob/v0.26.0/tree_sitter/binding/point.c#L35-L40)
returns a borrowed tuple-item reference from a getter requiring a new reference. Upstream
[fix afb3836](https://github.com/tree-sitter/py-tree-sitter/commit/afb3836bda1afd853d210468c94d257ade93e814)
and [issue505](https://github.com/tree-sitter/py-tree-sitter/issues/505) confirm premature freeing of
non-immortal row integers. Exact corruption of our faulting pointer was not separately decoded.

The local counterfactual changed **only** the binding package to published0.25.2: the identical
49-file command changed from repeated signal-10 to return0 in25.3seconds,314real embeddings and
1743pairs. Final normal-environment runs after the dependency correction reproduced the same JSON;
Python output stayed byte-identical. This is why the dependency and runtime guard pin0.25.2.
No GC disable, import-order retry, special filename, increased budget, vendored unreleased C patch
or new process manager was added. The unpatched0.26.0 is rejected before invoking native getters;
new parser versions require contract/regression validation. Tests cover long lines/Unicode and
binding kinds, including method properties versus lexical named-function bindings.

Compact machine-readable verification: [JS_VERIFICATION.json](JS_VERIFICATION.json). Raw local logs,
model pair reports and crash evidence stay private; candidate counts are not accuracy metrics.

Setup and commands: [README](../README.md#javascript--typescript-alongside-python).
