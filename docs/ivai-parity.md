# IVAI integration parity

```text
IVAI main 58f4a959 (native/security fixes) ─┐
                                         ├─ one standalone owner
standalone main 72bc52a (scan improvements)┘
```

Reviewed all scanner changes since IVAI merge-base `a1da2fa5`: `__init__.py`, `cli.py`, `detectors/security.py`, `mutation.py`; the six modified scanner suites; and new source-identity/copy-assets contracts.

| Source intent | Result |
|---|---|
| Native mutmut 3.7 exact-file selection and process/source identity | Preserved in `mutation.py`; native configuration replaces 3.4 compatibility patches. |
| Failed coverage baseline, repeated-pass isolation, macOS proxy safety, tracked-child reaping | Existing adapters preserved; actual native CLI tests distinguish healthy mutation from broken baseline. |
| Security command and dynamic-import authorization | Preserved in `cli.py` and `detectors/security.py`, alongside standalone assertion checks. |
| NUL/newline CLI output and subprocess owner coverage | Updated contracts and native fixture helpers ported. |
| IVAI application/MCP/async/generator/method/default contracts | Remain in IVAI; installed scanner self-target sources are copied explicitly. |
| IVAI mutation assets/test mappings | Explicit project configuration, not generic-package hardcoding. |
| Standalone failed-measurement protection, configurable roots and fail-fast semantic audit | Retained. No compatibility `scripts.deadcode` package introduced. |

Subtraction first (Occam/Tesler): remove vendored ownership and 3.4 compatibility adapters, not necessary safety handling; reuse existing commands/configuration rather than add parallel surfaces. Source: Laws of UX vault notes for Occam's Razor and Tesler's Law (`https://lawsofux.com/`). Detector removal reference: `/Users/vladnikulin/code/from GH/aislop/src/engines/code-quality/unused-removal-ast.ts:23-77` (side effects preserved before deletion). This transfer ports approved source, not a new detector design.

Verification: upstream baseline **464 passed**; integrated package **507 passed**, including the real mutmut CLI with **1 killed, 0 survived** and rejection of a failing baseline. These fixture numbers are wiring checks, not a production-code mutation benchmark. Native E9/F lint and wheel build passed. Duplicate-definition and repository-import-root regressions failed before their corrections. IVAI's full security shell pipeline is retained separately; `deadcode security` replaces only its AST stage.
