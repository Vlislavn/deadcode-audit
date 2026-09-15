# Provenance

Extracted from `a private source monorepo`, `scripts/deadcode/`, source commit
`e4acda7dacad690894e454b77c41d233e3edf2cd` (last module change).
All 32 source files and 17 dedicated test files were retained for adaptation.

No blanket open-source license was present in the source monorepo at extraction time.
No license for original the source monorepo contributions is invented by this extraction.
Some detectors/scoring explicitly identify ports from scanaislop/aislop;
the upstream MIT notice is retained in THIRD_PARTY_NOTICES.md.

The project is licensed under CC BY-NC 4.0 (non-commercial use with attribution required);
see LICENSE. MIT-licensed upstream portions retain commercial use under their own terms.

## Migration and validation — 2026-09-15

the source monorepo's `scripts/deadcode/` and 17 dedicated test files were removed from the migration branch;
implementation and those tests live here. Clients import `deadcode_audit` and pin its Git revision;
there is no compatibility shim at the old module path. the source monorepo retains `.deadcode.yml` policy and
consumer tests; the dispatcher consumer uses the installed package with `DEADCODE_PYTHON` as the
interpreter override instead of `SOURCE_REPO`.

Validation at the tests-only commit: **450 local tests passed**, including real CLI mutation checks (a killed
mutant passes; a failing clean baseline is rejected). the source monorepo: five consumer tests passed, including
invocation from another working directory. A built wheel was installed outside the source monorepo and its CLI
executed. Consumers currently pin the initial implementation commit; later commit adds
tests only.

Real CodeBERT inference was exercised on a target repository's configured runtime corpus: 275 files, 2496
functions, 1940 embedded; 142 seconds and approximately 1.1 GiB sampled peak RSS. These
measurements describe that input snapshot, not arbitrary repositories or a full scan of every
target file. Model revision: `3b0952feddeffad0063f274080e3c23d75e7eb39`.

The recorded [GitHub run](https://github.com/Vlislavn/deadcode-audit/actions/runs/34940212252)
ended with `startup_failure` before jobs; no CI pass is claimed and the API did not expose the
cause. No billing/quota settings were changed. Native Windows mutation is unsupported; no Windows
mutation support is claimed.
