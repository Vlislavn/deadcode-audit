# Changelog

## [Unreleased]

### Changed
- Preserve IVAI's security command and dynamic-import authorization checks alongside standalone assertion and fail-fast rules.
- Use native mutmut 3.7 configuration while retaining failed-baseline rejection, test-pass isolation, source/process identity, macOS proxy safety and tracked-child reaping.
- Port updated CLI/native-owner regression contracts; keep repository mutation assets and roots configurable. Repository import roots no longer evict virtual-environment dependencies.
- Permit Transformers 5.x so consuming repositories can retain their patched dependency constraints.
- Remove an overwritten partial detector definition that broke the package's native lint gate; add a duplicate-definition regression contract.
