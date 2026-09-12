# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.4] - 2026-09-12

### Fixed

- A transcript that exists in both the shared store and an account home as two separate
  files no longer makes the whole account unusable. Codex rewrites a rollout in place when
  it migrates one to a newer on-disk format, which breaks the hard link the two homes
  shared; sharing now leaves both copies untouched, reconciles every other transcript, and
  reports the count in `sc sessions status` instead of refusing the account.
- The account picker keeps one status line per account. A failed usage reading used to be
  printed verbatim, and Codex quotes the entire HTTP response body, so a single expired
  token scrolled the other accounts off the screen. An expired token now reads as the
  refresh it is, since Super Codex reads usage without refreshing the token and Codex
  refreshes it when the account next runs.

## [0.9.3] - 2026-09-10

### Fixed

- Unknown top-level commands now show a short `Unknown command` message instead of an
  argparse error that lists every command.

## [0.9.2] - 2026-09-10

### Added

- `sc sessions purge` previews and, after typed confirmation, permanently removes Codex
  session and archived-session transcripts older than 15 days by default. Use
  `--older-than DAYS` to choose the retention window or `--dry-run` to preview only.

## [0.9.1] - 2026-09-10

### Fixed

- `sc upd` is now a supported shorthand for `sc update`.

## [0.9.0] - 2026-09-08

### Changed

- The account picker no longer opens on an account whose limits are spent. The main account
  keeps the cursor while it has limits left; once a window is fully spent the cursor starts
  on the next account that can run, wrapping so accounts listed before the bound one stay
  reachable, and stays on main when no other account can run. Selection is still an explicit
  keypress: only the starting highlight moves, and no account is ever switched to on its own.
  Accounts report a new `limits spent` state alongside `ready`, `login needed`, and
  `unavailable`, so the reason the highlight moved is visible. An account close to its limit
  is still treated as usable, as is one holding unlimited credits; only a window at or past
  100%, a reported rate-limit or spend-control state, or an exhausted individual spend limit
  counts as spent. An account whose status could not be read is treated as unknown rather
  than spent, so a slow probe or a lapsed login never quietly hands the cursor to a
  different identity.

## [0.8.1] - 2026-09-07

### Fixed

- Shared Codex assets and custom session names now reconcile again after every normally
  exiting `sc`-launched Codex process. Sessions created under an isolated account reach
  the shared store without a later manual `sync` or repeated `merge`; sharing remains the
  default, and `merge` remains only the one-time upgrade step for pre-sharing history.

## [0.8.0] - 2026-09-06

### Added

- Shared Codex session history. Every account, and a bare `codex`, now read and write one
  session history by default, so `sc resume <id>` works whichever account you launch and
  the `codex resume` picker offers the same sessions everywhere. A shared transcript is
  one file with a hard link in each account home, so sharing costs no disk space and
  copies nothing.
- `sc sessions status|merge|share|split|sync|include|exclude`. `status` reports where each
  account records sessions and how many transcripts predate sharing, `merge --dry-run`
  previews unifying them, and `share`/`split` turn sharing on or off globally or for one
  account.
- Prompt history, attachments, and Codex configuration follow the shared store alongside
  transcripts; `sc sessions include` and `sc sessions exclude` change the set. Transcripts,
  attachments, and prompt history are hard linked. Configuration is copied instead,
  because Codex rewrites `config.toml` whenever a setting changes and a hard link would
  silently break; the shared Codex home is its source of truth and the copy keeps a
  timestamped backup of what it replaced.
- `sc resume <id>` finds a session recorded by another account and links that transcript
  into the account being launched, so an id resolves even for an account deliberately
  split off. Only transcript filenames are inspected.
- A `sessionSharing` block in `sc status --json` and a `Sessions:` line in `sc status`.
- Active custom session names are reconciled through Codex's app-server during `sc sessions
  sync` and `sc sessions merge`; provider SQLite databases remain account-local.

### Changed

- Codex transcripts are shared across accounts again, this time without breaking thread
  forking. Codex canonicalizes rollout paths and refuses any that resolve outside
  `CODEX_HOME`, which is what made the symbolic links used before 0.6.0 fail; hard links
  give each home a real name for the same file and satisfy that check.
- Upgrading shares sessions recorded from the upgrade onward and leaves earlier ones where
  they are, so an upgrade does not silently reshape the resume picker. `sc sessions status`
  reports the backlog and `sc sessions merge` unifies it.
- Session directories the shared store already owns are no longer re-permissioned on every
  launch; a directory Super Codex creates is still private.
- A hard-link failure now stops synchronization instead of silently creating a divergent
  copy, and copied configuration is replaced atomically with collision-safe backups.
- Every Codex profile uses the same injected status line with both five-hour and weekly
  usage remaining, model and reasoning, Git branch, and current directory.

### Security

- `auth.json` is never shared, read, or copied under any setting, and only the named asset
  groups are ever linked or copied. Account databases, logs, installation identifiers, and
  every other file in a provider home stay strictly per account.
- `sc sessions split` stops future sharing without unlinking anything an account can
  already see, because removing a hard link could remove the only remaining copy.

## [0.7.0] - 2026-09-06

### Added

- Model providers. `sc provider add|list|show|use|remove` defines OpenAI-compatible
  endpoints, and `sc --provider <name>` routes a single launch through one. The provider
  is rendered as a Codex profile written into whichever account home the launch uses, so
  one definition works from every account. `default` is reserved for the account's own
  authentication.
- `--provider` on `sc start`, `sc ask`, and `sc resume`, a `provider` field in
  `sc status --json`, and per-workspace provider bindings held in their own
  `providerBindings` map so that routing a workspace through a provider never pins which
  account that workspace uses. `sc unuse` releases both kinds of binding.

### Changed

- Configurations written before providers existed gain the `providers`,
  `providerDefaults`, and `providerBindings` keys on load; the schema stays at version 2 and older releases
  continue to read the file.

### Security

- Provider definitions store the name of the credential environment variable, never the
  credential. Generated Codex profiles are written atomically with `0600` permissions.


## [0.6.0] - 2026-08-24

### Fixed

- Isolated Codex profiles no longer link `sessions` and `archived_sessions` into the
  shared Codex home. Codex canonicalizes rollout paths and rejects any rollout that
  resolves outside `CODEX_HOME`, so the link made `/fork` fail with `rollout path ...
  must be in Codex home directory`. Existing links are converted once into real
  directories that hard link the shared transcripts, so previously indexed history stays
  available and forking works.

### Changed

- Codex transcripts are no longer shared across accounts. Each isolated Codex profile now
  records new sessions in its own provider home; a safe replacement home carries that
  profile's own transcripts forward.

## [0.5.0] - 2026-08-21

### Added

- Added `sc profile main <agent> <profile>` to designate a main account without moving
  provider state, mark it in profile listings and the picker, and route `sc main` to the
  designated Codex account.
- Added managed Claude jobs with quick, standard, and deep execution profiles, automatic
  background continuation, local status monitoring, and explicit cancellation.

### Changed

- Claude dollar budgets are now opt-in through an explicit `max_budget_usd` request or
  `SUPER_CODEX_CLAUDE_MAX_BUDGET_USD`; no implicit dollar limit is passed to Claude.

## [0.4.0] - 2026-08-21

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
