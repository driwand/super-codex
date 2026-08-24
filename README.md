# Super Codex

A local, Codex-first control plane for using multiple Codex accounts and consulting Claude without leaving Codex.

> **Disclaimer:** This project is **FULLY vibe coded**. Review the source and test
> it in your own environment before relying on it.

Super Codex is a thin wrapper around the official Codex and Claude Code CLIs. Codex remains the main interface. Account credentials and local Codex session history stay isolated per account, and Claude is exposed to Codex as a read-only consultation tool.

## What it does

- Uses Codex as the default coding agent.
- Keeps an existing Codex login as `codex/main`.
- Supports as many as five Codex accounts named `main`, `2`, `3`, `4`, and `5`.
- Shows an arrow-key account picker with each account's identity and current limits when you run bare `sc`.
- Lets you designate any configured Codex profile as the main account.
- Lets you reorder accounts and globally switch bare `sc` between the picker and `main`.
- Keeps Codex session and archived-session history separate per Codex profile.
- Gives every `sc`-launched Codex session a read-only `ask_claude` MCP tool.
- Keeps longer Claude consultations running as monitored, cancellable background jobs.
- Keeps direct Claude launch available as an explicit fallback.
- Binds a project directory to a profile without modifying that project.
- Reads Codex account identity and limits through Codex's app-server protocol.
- Never reads, copies, decodes, exports, or swaps provider credential files.
- Has no runtime dependencies, telemetry, daemon, or background updater.

It is not an AI agent, model proxy, token broker, or automatic quota-rotation service.

## Requirements

- macOS or Linux
- Python 3.9 or newer
- [OpenAI Codex CLI](https://developers.openai.com/codex/cli/) for the primary workflow
- [Claude Code](https://code.claude.com/docs/en/setup) if you want Claude consultation or fallback

Windows is not currently supported. The interactive providers may support Windows independently, but Super Codex's live Codex status transport is tested on POSIX systems only.

## Install and update

The supported installation is a standalone executable from a public GitHub
Release. Download and inspect the dependency-free installer, then run it:

```bash
curl -fLO https://github.com/driwand/super-codex/releases/latest/download/install.py
python3 install.py
```

You can download `install.py` through a browser instead of using `curl`. The
installer uses only Python's standard library. It downloads the release manifest
and standalone `sc` asset, verifies its declared size, SHA-256 checksum, release
tag, commit, and embedded build metadata, then installs:

```text
~/.local/bin/sc
~/.local/bin/super-codex -> sc
```

It never uses `sudo`, a Python package manager, or a virtual environment, and it
does not edit shell startup files. If `~/.local/bin` is not on `PATH`, it prints
the directory you need to add. Existing unrelated commands are never overwritten.

Install an exact stable version or choose another destination when needed:

```bash
python3 install.py --version vX.Y.Z
python3 install.py --bin-dir /your/bin/directory
```

Release tags are the only installation channel; `main` remains development
source and the project is not published to PyPI. Contributors can run a checkout
without installing it:

```bash
./sc setup
```

The distribution name is `super-codex`. Installation provides both `sc` and `super-codex`; this guide uses the shorter `sc` command. The former `sa` name deliberately is not installed because macOS already reserves it for process accounting.

Updates are manual and explicit:

```bash
sc update --check
sc update
```

Install or roll back to an exact immutable release tag with:

```bash
sc update --version vX.Y.Z
```

An update is downloaded beside the installed executable, fully verified, and
atomically swapped into place. An interrupted or invalid download leaves the
current installation unchanged.

Confirm exactly what is running, including the requested Git revision and commit
when the installer metadata provides them:

```bash
sc version
sc version --json
```

Uninstall the standalone commands without deleting Super Codex configuration,
profiles, sessions, or provider state:

```bash
sc uninstall
```

## Quick start

```bash
sc setup
sc status --live
```

Your existing Codex login is `codex/main`. Add and authenticate another account using Codex's native login flow:

```bash
sc profile add codex 2 --label "Personal"
```

The label is optional; without one, this profile is labeled `Codex 2`. The command creates the isolated profile and immediately starts Codex's native login flow.

Running the same command for an existing isolated profile starts a replacement login in a fresh provider home. Super Codex warns before login and atomically selects that home, including any new label, only after the provider reports verified authentication. Cancelling, failing, or exiting without authentication removes the unused candidate and leaves the existing provider home untouched. After a successful replacement, the previous provider home is retained so an already-running session is not disrupted, but future launches use the replacement. Credentials remain managed exclusively by the provider's native login flow; Super Codex never reads or copies them.

Give the profiles meaningful labels and designate the account used by `sc main`:

```bash
sc profile label codex main "Work"
sc profile label codex 2 "Personal"
sc profile main codex 2
```

The picker marks the designated account as `(main)` and preselects it when no workspace binding applies. This changes profile routing only: it never moves or swaps provider files. The storage profile named `codex/main` remains the original shared Codex home, and `sc 1` continues to launch it directly.

Confirm the identities and limits:

```bash
sc status --live
```

Run bare `sc` to fetch each configured account's username and current limits, then choose with the arrow keys and Enter:

```bash
sc
```

Press `1`–`5` in the picker for a quick selection. You can also bypass the picker directly: `sc main` launches the designated main Codex account, while `sc 1` through `sc 5` launch fixed storage profiles.

```bash
sc main
sc 1
sc 2
```

The picker is the default global behavior for Codex selections. Make bare `sc`
launch the profile selected by the current workspace or global binding without a
picker, or restore the picker, with:

```bash
sc config mode main
sc config mode select
```

Interactive Codex sessions launched by `sc start`, bare `sc`, or `sc resume` use
Codex's native status line to show the model and reasoning level, weekly usage
remaining, the current Git branch, and the working directory. Codex omits usage
or branch fields when their data is unavailable. The override applies only to the
launched session and does not modify the profile's `config.toml`.

Then ask naturally inside Codex:

```text
Ask Claude to review this change and tell me what Codex may have missed.
```

Codex calls the locally authenticated Claude CLI through Super Codex's MCP server, receives Claude's response, and remains responsible for the final decision and any edits.

Super Codex injects a session-only Codex `developer_instructions` override that routes
natural requests such as “make Claude review these changes” directly to `ask_claude`.
It forbids repository pre-inspection, direct Claude CLI fallback, manual MCP JSON, and
automatic retries for those requests. Existing project `AGENTS.md` instructions still
apply normally. Restart Codex after upgrading because the routing instruction and MCP
tool are loaded when the session starts.

Use `/mcp` once after starting Codex to confirm `super_codex_claude` is active. If
you upgraded Super Codex while Codex was already open, restart that Codex session;
MCP tools are discovered when the session starts.

## Common workflows

Bind the current project to one Codex profile. All profiles see the same local Codex resume history:

```bash
sc use codex main
sc use codex 2
```

Resume a session with whichever account you want to use for the next turn:

```bash
sc resume --profile main
sc resume --profile 2
sc resume --profile 2 --last
```

Do not resume the exact same session concurrently from two accounts; both processes would append to the same local transcript.

List all project bindings:

```bash
sc bindings
sc bindings --json
```

The normal Claude workflow stays inside Codex:

```text
Ask Claude for an independent review of the current implementation.
```

Claude is restricted to `Read`, `Glob`, and `Grep` during MCP consultations. It cannot run Bash or edit files. Codex evaluates the advisory response and performs any approved work itself.

You can still launch Claude directly as an explicit fallback:

```bash
sc start --agent claude
sc ask --agent claude "Review the current changes"
```

The direct Claude command above uses no Codex tokens. For a low-overhead one-shot
consultation routed through Codex, select low reasoning explicitly:

```bash
sc ask --reasoning low "Ask Claude to review the current change"
```

Make Claude the default for only the current project:

```bash
sc use claude main
```

Return the project to Codex:

```bash
sc use codex main
```

Resume native provider sessions:

```bash
sc resume --last
sc resume --agent claude --last
```

Check account and installation health:

```bash
sc usage --all
sc doctor --live
sc status --live --json
```

Forward a provider-specific argument explicitly after `--native`:

```bash
sc start --agent claude --native --permission-mode plan
```

Super Codex never adds permission-bypass flags. Arguments after `--native` are passed through exactly as supplied, so review them with the same care you would use when invoking the provider directly.

## Account and session model

| Profile | Default storage behavior |
| --- | --- |
| `codex/main` | Inherits the normal Codex environment and existing login; owns `~/.codex` and its session directories |
| `codex/2` through `codex/5` | Use private `CODEX_HOME` directories for authentication, configuration, and session directories |
| `claude/main` | Inherits the normal Claude Code environment and existing login |

Numbered Codex profiles are isolated by default. Add only the accounts you need:

```bash
sc profile add codex 2 --label "Personal"
sc profile add codex 3
```

The five storage identifiers are exactly `main`, `2`, `3`, `4`, and `5` (`1` is accepted as a command-line alias for the shared `main` storage profile). Designate any configured account as the logical main account, or reorder every configured profile in the picker by listing each one exactly once:

```bash
sc profile main codex 2
sc profile order codex main 3 2
```

Changing the logical main account also changes the global Codex default when Codex is the active global agent. Existing workspace bindings remain explicit and are not rewritten.

Codex officially supports relocating state with `CODEX_HOME`. Super Codex preserves
an exported shared home across direct and nested launches and keeps isolated provider
homes separate, including their `sessions` and `archived_sessions` directories. Codex
canonicalizes rollout paths and refuses any rollout that resolves outside `CODEX_HOME`,
so linked session directories break thread forking. Session directories linked by
releases before 0.6.0 are converted once into real directories that hard link the shared
transcripts, so history already indexed by that profile stays available.

Isolated Claude profiles use `CLAUDE_CONFIG_DIR`, which works in current Claude Code
releases but is not documented as a stable public interface. Super Codex remembers
whether that variable was exported before entering an isolated profile so a nested
shared-profile launch restores the original value or its absence; the default Claude
profile therefore remains shared.

Isolation covers provider configuration, credentials, logs, account-specific databases, and Codex transcripts stored in that provider home. Each account records new sessions in its own home, so a session started under one account is resumed under that same account. It does not isolate operating-system state such as Git configuration, SSH keys, keychains, browser sessions, or files accessible to the launched agent.

## Claude inside Codex

Every Codex command launched through `sc` receives an ephemeral MCP configuration override for Super Codex's local STDIO server. This does not edit `~/.codex/config.toml`. Inside Codex, `/mcp` shows `super_codex_claude`, and Codex calls its `ask_claude` tool when you explicitly ask Claude or request a cross-model review. The server is marked required so a failed MCP startup is reported instead of silently falling back to shell probing.

The consultation runs `claude -p` with the prompt over standard input. Only Claude's read-only `Read`, `Glob`, and `Grep` tools are enabled. No permission-bypass flags are added, and Claude's output is treated as untrusted advisory text rather than an instruction to edit automatically.

Each request is a managed in-memory job. Super Codex waits up to 10 seconds for a fast
answer; slower work returns a job ID and continues without holding the original MCP call
open. Codex monitors the same job with `claude_job_status`, which reads only local state
and consumes no additional Claude usage, and can stop it with `cancel_claude_job`.
Completed job results remain available for 15 minutes, with at most 20 completed jobs
retained. Jobs never survive the live Codex session.

Super Codex assigns the first consultation a native Claude Code session ID. Later `ask_claude`
calls in the same live Codex session resume that ID, so follow-up questions retain
Claude's prior prompts, tool results, and answers. The ID is held only in the MCP
server's memory and is isolated by workspace and Claude profile. Starting a new Codex
process, including `sc resume`, starts a fresh Claude consultation context.

To deliberately discard the active context, ask Codex explicitly, for example:

```text
Ask Claude with fresh context to review the new implementation.
```

Codex sets `new_context=true` on that call. Super Codex never guesses topic boundaries,
and it replaces the active session ID only after the fresh call succeeds. Native Claude
sessions are [stored as plaintext by Claude Code](https://code.claude.com/docs/en/sessions) inside the selected Claude profile.
Super Codex never reads or copies those transcripts and does not delete them when a
context is replaced or the MCP server exits. Use Claude Code's own retention settings to
manage that provider-owned history.

Super Codex allows only one active consultation for the same workspace and Claude profile. The lock is owned by the operating system and is released when the process exits, so a retry cannot start a second Claude process while the first remains live. MCP cancellation, explicit job cancellation, client disconnect, timeout, `SIGINT`, and `SIGTERM` all cancel the consultation and terminate its complete Claude process group. Changing the designated main Claude profile affects new jobs; an active job stays with the profile under which it started.

For requests mentioning current changes, a diff, staged work, or uncommitted work,
Super Codex obtains Git status plus staged and unstaged diffs itself using argument-array,
read-only Git commands. External diff and text-conversion drivers are disabled. The
context is sent directly to Claude, so Codex does not need to inspect or relay the diff.

Three execution profiles provide predictable time and turn limits. `standard` is the
default; Codex selects `quick` or `deep` only when the user explicitly requests it.
Explicit per-request overrides may select any timeout from 10 seconds through 30 minutes
and any turn limit from 1 through 50.

| Profile | Wall-clock timeout | Agent turns |
| --- | --- | --- |
| `quick` | 1 minute | 3 |
| `standard` | 5 minutes | 10 |
| `deep` | 30 minutes | 30 |

Other resource controls remain deliberately bounded:

| Control | Default | Override |
| --- | --- | --- |
| Claude output per model response | 4,096 tokens | `SUPER_CODEX_CLAUDE_MAX_OUTPUT_TOKENS` (512-16,384) |
| Claude dollar budget | none | explicit `max_budget_usd` or `SUPER_CODEX_CLAUDE_MAX_BUDGET_USD` (0.01-10) |
| Claude effort | `low` | `SUPER_CODEX_CLAUDE_EFFORT` (`low` through `max`) |
| Parallel Claude read tools | 1 | Not configurable through Super Codex |
| Git change context sent to Claude | 24,000 characters | Not configurable |
| Text returned to Codex | 8,000 characters | Not configurable |

`SUPER_CODEX_CLAUDE_TIMEOUT_SECONDS` and `SUPER_CODEX_CLAUDE_MAX_TURNS` explicitly
replace the selected profile's defaults. The output-token setting follows Claude Code's
documented `CLAUDE_CODE_MAX_OUTPUT_TOKENS` interface. When explicitly supplied, the
dollar budget uses Claude Code's client-side cost estimate; it is a stopping guard, not
an exact prediction of subscription quota impact. The final 8,000-character truncation
protects Codex context only and does not refund or prevent tokens Claude already generated.

If a consultation appears slow, monitor the returned job or cancel it. Do not start a replacement while its status is uncertain; Super Codex will reject a duplicate for that workspace and profile.

Resuming Claude preserves useful context but increases the amount of prior conversation
Claude may need to process. Start a fresh context for unrelated work instead of allowing
one review thread to grow indefinitely.

## Configuration

The source of truth is:

```text
~/.config/super-codex/config.json
```

Use the CLI to inspect the schema-versioned registry and change bare-command behavior:

```bash
sc config path
sc config show
sc profile main codex 2 # designate codex/2 as the main Codex account
sc config mode          # print select or main
sc config mode main     # bare sc launches the selected binding directly
sc config mode select   # Codex bindings open the live picker
```

The current config schema is version 2. This profile model is a clean break: schema-v1 registries and the former `second` profile name are not migrated. Super Codex always uses `~/.config/super-codex`; an old `~/.config/super-agent-control` directory is ignored and left untouched. Provider credential files are never read or copied by Super Codex.

Override the state root for testing or portable setups:

```bash
SUPER_AGENT_HOME=/path/to/state sc status
```

The registry contains profile metadata and workspace bindings only. It contains no provider tokens or API keys. Isolated provider credentials are written by the provider's own native login command into that profile's private home.

## Usage reporting

Codex usage is fetched through the local Codex app-server. Depending on the account, this may report rolling windows, credits, or an organization spend control. The API is experimental, so failures are reported as unavailable rather than guessed.

Claude Code does not expose a supported standalone usage command. Run `/usage` inside Claude for its authoritative limits.

## Security and provider terms

This project is intended for legitimate separation of personal, work, client, or organization accounts. It does not automatically rotate accounts, choose accounts based on remaining quota, evade rate limits, or bypass provider safeguards. You select the account explicitly. You are responsible for using each account in accordance with the applicable provider terms and organizational policies.

See [SECURITY.md](SECURITY.md) for the threat model and vulnerability reporting process.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes.

## Acknowledgments

The implementation is original, but its design was informed by publicly documented ideas in:

- [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc)
- [Ducksss/codex-profiles](https://github.com/Ducksss/codex-profiles)
- [realiti4/claude-swap](https://github.com/realiti4/claude-swap)
- [Loongphy/codex-auth](https://github.com/Loongphy/codex-auth)

No source code from those projects is bundled or copied here, and they are not runtime dependencies.

## Trademark notice

Codex, ChatGPT, and OpenAI are trademarks or registered trademarks of OpenAI. Claude and Anthropic are trademarks or registered trademarks of Anthropic. Super Codex is an independent community project and is not affiliated with, endorsed by, or sponsored by OpenAI or Anthropic.

## License

Licensed under the [MIT License](LICENSE).
