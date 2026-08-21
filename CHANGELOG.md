# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added up to five Codex accounts named `main`, `2`, `3`, `4`, and `5`.
- Added a default arrow-key account picker that shows live identity and limit details.
- Added direct `sc main` and `sc 2` through `sc 5` launch shorthands.
- Added configurable picker ordering and a global `select`/`main` bare-command mode.
- Added a native Codex status line for interactive sessions with model and reasoning,
  weekly usage remaining, Git branch, and working directory.
- Added a package-manager-free standalone GitHub Release installer with verified,
  atomic update, rollback, and uninstall workflows.
- Added `sc version` with machine-readable installed-source and Git provenance.
- Added gated GitHub Releases for annotated, version-matched tags.
- Added fail-closed pre-commit, pre-push, CI, and release scans using a
  checksum-pinned Gitleaks binary plus repository-specific privacy checks.
- Added gitignored, one-way sensitive-term registration so private names can be
  blocked without storing their plaintext in the repository.

### Changed

- Replaced the former two-account configuration with schema version 2. Version 1 is
  intentionally not migrated.
- `sc profile add` now creates the profile and immediately starts the provider's native
  login flow; `--label` remains optional.
- Adding an existing isolated profile now authenticates in a fresh provider home, warns
  before replacement, and atomically commits it only after native authentication succeeds.

## [0.3.2] - 2026-08-20

### Fixed

- Standardized the Claude MCP input on a `request` field while preserving the former
  `prompt` field as a server-side compatibility alias.

## [0.3.1] - 2026-08-20

### Fixed

- Avoided fragile parsing of Claude's JSON stdout by assigning a native session UUID
  before each new consultation and returning Claude's plain-text response.

## [0.3.0] - 2026-08-20

### Added

- Continued Claude consultations within each live Codex session by capturing and
  resuming Claude Code's native session ID.
- Added explicit `new_context=true` support for starting an unrelated Claude
  consultation without guessing topic boundaries.

### Changed

- Renamed the MCP tool to `ask_claude` and made direct native routing explicit.
- Required and allow-listed the Claude MCP tool for Codex launches to prevent silent shell fallback.
- Added an explicit Codex `--reasoning` option and bounded Claude consultation output.
- Serialized Claude consultations per workspace and profile to reject duplicate retries.
- Added cancellation-aware MCP workers and complete Claude process-group cleanup.
- Added conservative Claude output-token, estimated-budget, effort, tool-concurrency,
  and wall-clock controls.
- Added a supported Codex `developer_instructions` routing layer so natural Claude
  requests use the native MCP tool instead of repository inspection or shell fallback.
- Added automatic, bounded, read-only Git change context for Claude change reviews.

## [0.2.0] - 2026-08-20

### Added

- Shared local Codex resume history across isolated Codex account profiles without sharing credentials.
- A dependency-free local MCP server that lets Codex consult Claude with read-only workspace tools.

### Changed

- Renamed the distribution and commands to Super Codex: `super-codex` and `sc`.

## [0.1.0] - 2026-08-19

### Added

- Codex-first interactive, one-shot, and resume delegation.
- Shared primary and isolated additional Codex profiles.
- Claude Code fallback delegation.
- Native login and authentication status flows.
- Per-workspace profile bindings.
- Live Codex account and limit reporting through app-server.
- Machine-readable profile, binding, and status output.
- Profile display labels and guided first-run setup.
- Private state permissions, atomic registry writes, and symlink-resistant managed paths.
- Open-source license, security policy, contribution guide, packaging metadata, and CI.
