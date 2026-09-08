# Super Codex

Switch between Codex accounts and ask Claude for a second opinion, without leaving Codex.

Super Codex wraps the official Codex and Claude Code CLIs. Run `sc` to pick an account, see its remaining limits, and start working. Your Codex session history is shared across accounts by default, so you can pick up where you left off.

- **Multiple accounts:** keep up to five Codex logins and choose which one to use.
- **Claude reviews:** ask Claude to review your work inside a Codex session.
- **Project defaults:** choose an account or model provider for each workspace.

No runtime dependencies, telemetry, or automatic account switching.

> This project is fully vibe coded. Review the source and test it in your environment before relying on it.

## Install

Requires **macOS or Linux**, **Python 3.9+**, and the [Codex CLI](https://developers.openai.com/codex/cli/). Install and authenticate [Claude Code](https://code.claude.com/docs/en/setup) if you want Claude reviews.

Download the installer, inspect it, then run it:

```bash
curl -fLO https://github.com/driwand/super-codex/releases/latest/download/install.py
python3 install.py
```

This installs `sc` and its alias `super-codex` in `~/.local/bin`. If that directory is missing from your `PATH`, follow the installer's instructions. No `sudo` or package manager is needed.

## Start using it

```bash
sc setup
sc
```

Your existing Codex login becomes `codex/main`. Bare `sc` opens an account picker with identities and current limits. Use the arrow keys and Enter to choose.

Add another account with Codex's native login flow:

```bash
sc profile add codex 2 --label "Personal"
```

You can add accounts `3`, `4`, and `5` the same way. Credentials stay in their provider-managed homes; Super Codex never reads or copies credential files.

## Everyday commands

| Command | What it does |
| --- | --- |
| `sc` | Open the account picker |
| `sc 2` | Launch account 2 directly |
| `sc main` | Launch your designated main account |
| `sc profile main codex 2` | Make account 2 your main account |
| `sc use codex 2` | Set account 2 as this project's default |
| `sc resume --profile 2 --last` | Resume the last session with account 2 |
| `sc status --live` | Show account identities and limits |
| `sc doctor --live` | Check setup and provider health |

Prefer to skip the picker? Run `sc config mode main` to launch the project's default account directly. Restore it with `sc config mode select`.

Session sharing lets you continue work with another account. Avoid opening the same session from two accounts at once, since both would write to the same transcript.

## Ask Claude for a review

Inside a Codex session launched with `sc`, ask:

```text
Ask Claude to review the current changes and tell me what you may have missed.
```

Claude can read and search files during the review, but cannot run shell commands or edit files. Codex receives the advice and handles any edits. Longer reviews run as monitored jobs, and follow-up questions retain Claude's context for that live Codex session.

Use `/mcp` inside Codex to check that `super_codex_claude` is active. Restart your Codex session after upgrading to load the latest integration.

To use Claude directly instead:

```bash
sc start --agent claude
```

## Update or uninstall

```bash
sc update --check    # Check for a release
sc update           # Install it
sc version          # Show the installed build
sc uninstall        # Remove commands; keep your configuration and sessions
```

## More details

- [Accounts and isolation](docs/usage.md#account-and-session-model)
- [Session sharing and separate histories](docs/usage.md#session-history)
- [Custom model providers](docs/usage.md#model-providers)
- [Claude context, timeouts, and budgets](docs/usage.md#claude-inside-codex)
- [Installation options and rollback](docs/usage.md#install-and-update)
- [Configuration](docs/usage.md#configuration) and [usage reporting](docs/usage.md#usage-reporting)
- [Security](SECURITY.md), [contributing](CONTRIBUTING.md), and [changelog](CHANGELOG.md)

Codex's usage API is experimental; unavailable limits are reported as such. Account isolation covers provider state, not your operating-system account. See the reference for the full behavior and limitations.

Super Codex is an independent community project, unaffiliated with OpenAI or Anthropic. Codex, ChatGPT, OpenAI, Claude, and Anthropic are trademarks of their respective owners. Licensed under [MIT](LICENSE).
