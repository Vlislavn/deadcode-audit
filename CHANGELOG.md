# Changelog

## [Unreleased]

### Changed
- `deadcode overlaps` is now two-pass by default: pass 1 deterministic struct+API (unchanged), pass 2 CodeSage-small-v2 (pinned revision, embed-only) adds type-4 candidates as `embed_pairs`. Falls back to pass 1 with one warning when the model or extras are missing; `--no-embed` skips pass 2.

### Removed
- `--embed` and `--embed-topk` (the hybrid and two-stage scoring did not pass the q162 rule).
