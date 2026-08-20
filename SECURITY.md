# Security Policy

## Supported versions

Security fixes are currently applied to the latest `0.3.x` release line.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue. After this repository is published on GitHub, use its private **Security advisories** reporting flow. Until then, contact the maintainer privately through the contact channel on the repository owner's profile.

Include the affected version, operating system, reproduction steps, expected impact, and whether provider credentials or cross-profile state may be exposed. Do not include real tokens, credential files, or private session transcripts.

## Security model

Super Codex:

- does not read, copy, decode, export, or swap provider credential files;
- invokes each provider's native login and authentication-status commands;
- uses separate provider homes for isolated account credentials and configuration;
- intentionally shares only Codex `sessions` and `archived_sessions` directories through validated, user-owned links;
- refuses to replace an unexpected link or a non-empty isolated session directory;
- exposes Claude to Codex through a local MCP tool limited to `Read`, `Glob`, and `Grep`;
- serializes Claude consultations per workspace and profile and terminates the complete
  Claude process group on cancellation or timeout;
- applies conservative Claude effort, output-token, estimated-budget, and wall-clock
  limits without claiming that client-side estimates equal provider quota accounting;
- gathers optional Git review context with read-only argument arrays, disabled external
  diff/text-conversion drivers, and a fixed context-size cap;
- creates managed state directories with mode `0700` and registry files with mode `0600` on POSIX systems;
- rejects symlinked registry/profile roots and opens its registry without following symlinks;
- uses argument arrays rather than shell command strings;
- never adds permission-bypass flags;
- has no telemetry, daemon, cloud service, or runtime dependencies.

Native provider CLIs write their own credentials and session state into the selected provider home. Their storage formats, keychain behavior, network traffic, model execution, and permission systems remain outside this project's security boundary.

Codex session transcripts are deliberately shared across Codex profiles so either authenticated account can resume them. Do not resume the same session concurrently from multiple accounts. Claude consultations use Claude Code's native plaintext session history within the selected Claude profile; Super Codex keeps only the opaque session ID in process memory and never reads or copies the transcript. Claude consultation output is advisory and must be verified by Codex before changes are made. Never retry a consultation until the original call has completed, failed, or been cancelled; an uncertain or yielded execution is not evidence of failure.

Profile isolation does not isolate operating-system credentials or resources shared by child processes, including Git configuration, SSH keys, keychains, browser sessions, environment variables, and readable workspace files.

## Operational guidance

- Install Codex and Claude Code from their official distribution channels.
- Review project instructions and native arguments before launching an agent in an untrusted repository.
- Do not place `SUPER_AGENT_HOME` in cloud-synchronized or shared storage.
- Keep the state directory accessible only to your operating-system user.
- Do not commit `.test-state`, provider homes, or generated session data.
- Use provider accounts according to provider terms and organization policies.
