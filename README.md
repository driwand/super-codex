# Super Codex

A local, Codex-first control plane for choosing coding agents and account profiles without swapping credential files.

Super Codex is a thin wrapper around the official Codex and Claude Code CLIs. It keeps their native terminal interfaces, sessions, permission systems, and login flows while giving you one command for account visibility, project bindings, launches, resumes, and Codex usage limits.

## What it does

- Uses Codex as the default coding agent.
- Keeps an existing Codex login as `codex/main`.
- Provides an isolated `codex/second` profile for another legitimate account or organization.
- Keeps Claude available as an explicit fallback or reviewer.
- Binds a project directory to a profile without modifying that project.
- Reads Codex account identity and limits through Codex's app-server protocol.
- Never reads, copies, decodes, exports, or swaps provider credential files.
- Has no runtime dependencies, telemetry, daemon, or automatic updater.

It is not an AI agent, model proxy, token broker, or automatic quota-rotation service.

## Requirements

- macOS or Linux
- Python 3.9 or newer
- [OpenAI Codex CLI](https://developers.openai.com/codex/cli/) for the primary workflow
- [Claude Code](https://code.claude.com/docs/en/setup) if you want the Claude fallback

Windows is not currently supported. The interactive providers may support Windows independently, but Super Codex's live Codex status transport is tested on POSIX systems only.

## Install

From a checkout, run it without installing:

```bash
./sc setup
```

Install into an isolated tool environment with either `pipx` or `uv` if you already use one of them:

```bash
pipx install .
# or
uv tool install .
```

A standard virtual environment also works:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

The distribution name is `super-codex`. Installation provides both `sc` and `super-codex`; this guide uses the shorter `sc` command. The former `sa` name deliberately is not installed because macOS already reserves it for process accounting.

## Quick start

```bash
sc setup
sc status --live
```

Your existing Codex login is `codex/main`. Authenticate the isolated second profile using Codex's native login flow:

```bash
sc login --profile second
```

Give the profiles meaningful labels:

```bash
sc profile label codex main "Work"
sc profile label codex second "Personal"
```

Confirm the identities and limits:

```bash
sc status --live
```

Start the selected Codex profile:

```bash
sc
```

## Common workflows

Bind the current project to one Codex profile:

```bash
sc use codex main
sc use codex second
```

List all project bindings:

```bash
sc bindings
sc bindings --json
```

Use Claude once without changing the project binding:

```bash
sc start --agent claude
sc ask --agent claude "Review the current changes"
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

## Account isolation model

| Profile | Default storage behavior |
| --- | --- |
| `codex/main` | Inherits the normal Codex environment and existing login |
| `codex/second` | Uses a private `CODEX_HOME` under Super Codex's state directory |
| `claude/main` | Inherits the normal Claude Code environment and existing login |

Additional profiles are isolated by default:

```bash
sc profile add codex client --label "Client"
sc login --agent codex --profile client
```

Codex officially supports relocating state with `CODEX_HOME`. Isolated Claude profiles use `CLAUDE_CONFIG_DIR`, which works in current Claude Code releases but is not documented as a stable public interface; the default Claude profile therefore remains shared.

Isolation covers provider configuration, credentials, logs, and sessions stored in that provider home. It does not isolate operating-system state such as Git configuration, SSH keys, keychains, browser sessions, or files accessible to the launched agent.

## Configuration

The source of truth is:

```text
~/.config/super-codex/config.json
```

Override the state root for testing or portable setups:

```bash
SUPER_AGENT_HOME=/path/to/state sc status
```

The registry contains profile metadata and workspace bindings only. It contains no provider tokens or API keys. Isolated provider credentials are written by the provider's own native login command into that profile's private home. Existing installations using `~/.config/super-agent-control` continue to use that state directory until a new Super Codex state directory is created.

## Usage reporting

Codex usage is fetched through the local Codex app-server. Depending on the account, this may report rolling windows, credits, or an organization spend control. The API is experimental, so failures are reported as unavailable rather than guessed.

Claude Code does not expose a supported standalone usage command. Run `/usage` inside Claude for its authoritative limits.

## Security and provider terms

This project is intended for legitimate separation of personal, work, client, or organization accounts. It does not automatically rotate accounts, evade rate limits, or bypass provider safeguards. You are responsible for using each account in accordance with the applicable provider terms and organizational policies.

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
