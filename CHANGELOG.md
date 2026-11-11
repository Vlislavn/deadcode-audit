# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Add mixed Python/JS/TS scan/CI and function embeddings/overlaps with exact source identities. Reuse Oxlint1.90.0 and CodeBERT; pin Tree-sitter0.25.2 to avoid reproduced0.26.0 native corruption. Preserve Python-only results; no JS dead-export/Markdown/mutation claim.
- Exclude strict same-file JavaScript/TypeScript ancestor/descendant source-span pairs before overlap scoring; keep siblings, cross-file comparisons, embedding inputs, and Python-only behavior unchanged.
