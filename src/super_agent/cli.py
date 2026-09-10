import argparse
import json
import os
import select
import sys
import termios
import tty
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .adapters import (
    AdapterError,
    auth_command,
    auth_status,
    build_command,
    codex_limits_exhausted,
    codex_live_status,
    codex_set_thread_names,
    codex_thread_names,
    command_display,
    exec_command,
    executable,
    format_codex_live,
    run_command,
    version,
)
from .config import (
    AGENTS,
    CODEX_PROFILE_NAMES,
    DEFAULT_PROVIDER,
    PROVIDER_REASONING,
    PROVIDER_WIRE_APIS,
    SHARING_GROUPS,
    ConfigError,
    Store,
    render_codex_provider_profile,
)
from .mcp_server import serve as serve_mcp
from .provenance import print_installation_provenance
from .release import ReleaseError, run_uninstall, run_update


def parser():
    root = argparse.ArgumentParser(
        prog="sc",
        description="A Codex-first account and workspace control plane for coding agents.",
        epilog="Examples: sc | sc main | sc profile main codex 2 | sc profile order codex main 3 2",
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command")

    provenance = commands.add_parser("version", help="Show installed package provenance")
    provenance.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    update = commands.add_parser("update", help="Update a standalone release installation")
    update.add_argument("--check", action="store_true", help="Check without installing")
    update.add_argument("--version", metavar="vX.Y.Z", help="Install an exact release tag")

    commands.add_parser("uninstall", help="Remove a standalone release installation")

    commands.add_parser("setup", help="Check prerequisites and show first-run steps")

    start = commands.add_parser("start", help="Start the selected interactive agent")
    add_launch_arguments(start, prompt_required=False)

    ask = commands.add_parser("ask", help="Run one prompt and exit")
    add_launch_arguments(ask, prompt_required=True)

    resume = commands.add_parser("resume", help="Resume a native agent session")
    resume.add_argument("session", nargs="?")
    resume.add_argument("--last", action="store_true", help="Resume the latest session")
    add_selection_arguments(resume)
    resume.add_argument("--model")
    resume.add_argument(
        "--reasoning", choices=("minimal", "low", "medium", "high", "xhigh")
    )
    resume.add_argument("--provider")
    resume.add_argument("--dry-run", action="store_true")
    resume.add_argument("--native", nargs=argparse.REMAINDER, default=[])

    status = commands.add_parser("status", help="Show accounts, selection, and usage")
    status.add_argument("--live", action="store_true", help="Fetch live Codex limits")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    usage = commands.add_parser("usage", help="Fetch live Codex account limits")
    usage.add_argument("--all", action="store_true", help="Check every Codex profile")

    use = commands.add_parser("use", help="Bind an account to this workspace")
    use.add_argument("agent", choices=AGENTS)
    use.add_argument("profile")
    use.add_argument("--global", dest="globally", action="store_true")

    commands.add_parser("unuse", help="Remove this workspace's exact binding")

    bindings = commands.add_parser("bindings", help="List workspace-to-profile bindings")
    bindings.add_argument("--agent", choices=AGENTS)
    bindings.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    profiles = commands.add_parser("profiles", help="List account profiles")
    profiles.add_argument("--json", action="store_true")

    profile = commands.add_parser("profile", help="Manage account profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    add = profile_commands.add_parser(
        "add", help="Add a profile and run the provider's native login flow"
    )
    add.add_argument("agent", choices=AGENTS)
    add.add_argument("name")
    add.add_argument("--label")
    add.add_argument("--shared", action="store_true", help="Inherit the provider's normal home")
    label = profile_commands.add_parser("label", help="Change a profile's display label")
    label.add_argument("agent", choices=AGENTS)
    label.add_argument("name")
    label.add_argument("label")
    main_profile = profile_commands.add_parser("main", help="Designate the main account")
    main_profile.add_argument("agent", choices=AGENTS)
    main_profile.add_argument("name")
    order = profile_commands.add_parser("order", help="Set the profile picker order")
    order.add_argument("agent", choices=AGENTS)
    order.add_argument("names", nargs="+")

    provider = commands.add_parser("provider", help="Manage model providers")
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    provider_list = provider_commands.add_parser("list", help="List configured providers")
    provider_list.add_argument("--json", action="store_true")
    provider_add = provider_commands.add_parser(
        "add", help="Add or replace a model provider"
    )
    provider_add.add_argument("name")
    provider_add.add_argument("--base-url", required=True, metavar="URL")
    provider_add.add_argument("--env-key", required=True, metavar="VARIABLE")
    provider_add.add_argument("--model", required=True, metavar="SLUG")
    provider_add.add_argument("--label")
    provider_add.add_argument("--wire-api", choices=PROVIDER_WIRE_APIS, default="responses")
    provider_add.add_argument("--reasoning", choices=PROVIDER_REASONING)
    provider_remove = provider_commands.add_parser("remove", help="Remove a model provider")
    provider_remove.add_argument("name")
    provider_use = provider_commands.add_parser(
        "use", help="Route this workspace through a provider"
    )
    provider_use.add_argument("name")
    provider_use.add_argument("--global", dest="globally", action="store_true")
    provider_show = provider_commands.add_parser(
        "show", help="Show the Codex profile a provider generates"
    )
    provider_show.add_argument("name")

    sessions = commands.add_parser(
        "sessions", help="Share Codex session history across accounts"
    )
    session_commands = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_status = session_commands.add_parser(
        "status", help="Show where each account's transcripts live"
    )
    sessions_status.add_argument("--json", action="store_true")
    sessions_merge = session_commands.add_parser(
        "merge", help="Unify the transcripts recorded before sharing was enabled"
    )
    sessions_merge.add_argument("--profile")
    sessions_merge.add_argument(
        "--dry-run", action="store_true", help="Report what would be unified"
    )
    sessions_share = session_commands.add_parser(
        "share", help="Record new sessions into the shared store"
    )
    sessions_share.add_argument("--profile")
    sessions_split = session_commands.add_parser(
        "split", help="Keep an account's future sessions to itself"
    )
    sessions_split.add_argument("--profile")
    sessions_sync = session_commands.add_parser(
        "sync", help="Reconcile immediately instead of waiting for launch or exit"
    )
    sessions_sync.add_argument("--profile")
    sessions_include = session_commands.add_parser(
        "include", help="Add asset groups that follow the shared store"
    )
    sessions_include.add_argument("groups", nargs="+", choices=SHARING_GROUPS)
    sessions_exclude = session_commands.add_parser(
        "exclude", help="Keep asset groups per account"
    )
    sessions_exclude.add_argument("groups", nargs="+", choices=SHARING_GROUPS)
    sessions_purge = session_commands.add_parser(
        "purge", help="Preview and permanently remove old Codex transcripts"
    )
    sessions_purge.add_argument(
        "--older-than",
        type=int,
        default=15,
        metavar="DAYS",
        help="Purge transcripts older than this many days (default: 15)",
    )
    sessions_purge.add_argument(
        "--dry-run", action="store_true", help="Preview without asking or deleting"
    )

    login = commands.add_parser("login", help="Run the provider's native login flow")
    add_selection_arguments(login)
    login.add_argument("--dry-run", action="store_true")

    doctor = commands.add_parser("doctor", help="Check local installation and state")
    doctor.add_argument("--live", action="store_true")

    config = commands.add_parser("config", help="Show or change global configuration")
    config.add_argument(
        "action", choices=("path", "show", "mode"), nargs="?", default="path"
    )
    config.add_argument("value", nargs="?")
    commands.add_parser("mcp-server", help=argparse.SUPPRESS)
    return root


def add_selection_arguments(command):
    command.add_argument("--agent", choices=AGENTS)
    command.add_argument("--profile")


def add_launch_arguments(command, prompt_required):
    command.add_argument("prompt", nargs=None if prompt_required else "?")
    add_selection_arguments(command)
    command.add_argument("--model")
    command.add_argument(
        "--reasoning", choices=("minimal", "low", "medium", "high", "xhigh")
    )
    command.add_argument(
        "--provider",
        help=f"Model provider to route through ('{DEFAULT_PROVIDER}' for the account's own)",
    )
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--native", nargs=argparse.REMAINDER, default=[])


def selected(store, config, args, cwd):
    return store.selection(
        config,
        cwd,
        agent=getattr(args, "agent", None),
        profile=getattr(args, "profile", None),
    )


def launcher_command():
    invoked = Path(sys.argv[0])
    if invoked.name in ("sc", "super-codex") and invoked.is_file():
        return str(invoked.resolve())
    for name in ("super-codex", "sc"):
        path = executable(name)
        if path:
            return path
    source_launcher = Path(__file__).resolve().parents[2] / "sc"
    if source_launcher.is_file():
        return str(source_launcher)
    return "super-codex"


def _profile_row(store, config, agent, name, live):
    data = config["profiles"][agent][name]
    row = {
        "agent": agent,
        "profile": name,
        "label": data.get("label", name),
        "main": config["agentDefaults"][agent] == name,
        "isolation": data["isolation"],
        "authenticated": False,
        "authDetail": "",
        "live": [],
        "exhausted": False,
    }
    try:
        env = store.environment(agent, name, config)
        status = auth_status(agent, env)
    except ConfigError as exc:
        row["authDetail"] = f"profile unavailable: {exc}"
        row["error"] = str(exc)
        return row
    row["authenticated"] = status.authenticated
    row["authDetail"] = status.detail
    if live and agent == "codex" and status.authenticated:
        try:
            sqlite_home = store.home / "runtime" / "codex" / name
            live_status = codex_live_status(env, sqlite_home=sqlite_home)
            row["live"] = format_codex_live(live_status)
            row["exhausted"] = codex_limits_exhausted(live_status)
        except AdapterError as exc:
            row["live"] = [f"usage unavailable: {exc}"]
    return row


def profile_rows(store, config, live=False, only=None):
    targets = [
        (agent, name)
        for agent in AGENTS
        for name in store.ordered_profile_names(config, agent)
        if not only or (agent, name) in only
    ]
    if live and len(targets) > 1 and all(agent == "codex" for agent, _ in targets):
        # Independent status calls run together so one slow account does not hold up
        # every other account in the interactive picker.
        with ThreadPoolExecutor(max_workers=min(5, len(targets))) as executor:
            futures = [
                executor.submit(_profile_row, store, config, agent, name, live)
                for agent, name in targets
            ]
            return [future.result() for future in futures]
    return [_profile_row(store, config, agent, name, live) for agent, name in targets]


def _row_state(row):
    if row.get("error"):
        return "unavailable"
    if not row["authenticated"]:
        return "login needed"
    return "limits spent" if row.get("exhausted") else "ready"


def _startable(row):
    """Report whether a row can begin a session right now."""
    return _row_state(row) == "ready"


def _spent(row):
    """Report whether a row's limits are known to be spent.

    Only a positive usage reading counts. An account whose status could not be
    read — a slow probe, a login that has lapsed — is not spent, merely unknown,
    and an unknown account keeps the cursor rather than losing it to a guess.
    """
    return bool(row.get("exhausted"))


def _initial_index(rows, initial_profile):
    """Choose the row the picker opens on.

    The bound profile — main, unless this workspace is bound elsewhere — holds
    the cursor unless its limits are known to be spent, in which case the cursor
    moves to the next account that can run, wrapping so accounts listed before
    it stay reachable. The two conditions are deliberately not symmetric: the
    cursor leaves the bound account only on a definite reading, but lands only
    where a session can actually start. With nothing runnable the bound profile
    keeps the cursor and its state is shown instead.

    This only moves a highlight. Selection stays an explicit keypress, and no
    account is ever switched to on its own, which is the line AGENTS.md draws
    against automatic rotation around provider limits.
    """
    preferred = next(
        (
            index
            for index, row in enumerate(rows)
            if row["profile"] == initial_profile
        ),
        None,
    )
    if preferred is None:
        return next((index for index, row in enumerate(rows) if _startable(row)), 0)
    if not _spent(rows[preferred]):
        return preferred
    for offset in range(1, len(rows)):
        candidate = (preferred + offset) % len(rows)
        if _startable(rows[candidate]):
            return candidate
    return preferred


def _picker_lines(rows, selected_index):
    lines = [
        "Choose a Codex account",
        "Use ↑/↓ to move, Enter to select, 1-5 for quick select, or q to cancel.",
        "",
    ]
    for index, row in enumerate(rows):
        marker = ">" if index == selected_index else " "
        state = _row_state(row)
        main = " (main)" if row.get("main") else ""
        lines.append(f"{marker} {row['profile']:<4} {row['label']}{main}  [{state}]")
        details = row["live"] or ([row["authDetail"]] if row["authDetail"] else [])
        for detail in details:
            lines.append(f"      {detail}")
    return lines


def choose_profile(rows, input_stream=None, output_stream=None, initial_profile=None):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    if not rows:
        raise ConfigError("No Codex profiles are configured")
    if not input_stream.isatty() or not output_stream.isatty():
        raise ConfigError(
            "The account picker requires a terminal. Use `sc main`, `sc 2`, or "
            "set `sc config mode main`."
        )
    selected_index = _initial_index(rows, initial_profile)
    descriptor = input_stream.fileno()
    previous = termios.tcgetattr(descriptor)
    output_stream.write("\x1b[?1049h\x1b[?25l")
    output_stream.flush()
    try:
        # Cbreak gives us individual arrow-key bytes while preserving the
        # terminal's output processing. Raw mode disables ONLCR on POSIX and
        # makes successive lines drift to the right in many terminals.
        tty.setcbreak(descriptor)
        while True:
            rendered = "\r\n".join(_picker_lines(rows, selected_index))
            output_stream.write("\x1b[H\x1b[2J" + rendered + "\r\n")
            output_stream.flush()
            key = os.read(descriptor, 1)
            if key == b"":
                return None
            if key in (b"\r", b"\n"):
                return rows[selected_index]["profile"]
            if key in (b"q", b"Q", b"\x03", b"\x1b"):
                if key == b"\x1b":
                    suffix = b""
                    reached_eof = False
                    for _ in range(16):
                        if not select.select([descriptor], [], [], 0.05)[0]:
                            break
                        part = os.read(descriptor, 1)
                        if part == b"":
                            reached_eof = True
                            break
                        suffix += part
                        if (
                            len(suffix) >= 2
                            and suffix[:1] in (b"[", b"O")
                            and 0x40 <= part[0] <= 0x7E
                        ):
                            break
                        if len(suffix) == 1 and suffix[:1] not in (b"[", b"O"):
                            break
                    if reached_eof:
                        return None
                    if suffix == b"[A":
                        selected_index = (selected_index - 1) % len(rows)
                        continue
                    if suffix == b"[B":
                        selected_index = (selected_index + 1) % len(rows)
                        continue
                    if suffix:
                        continue
                return None
            if key in (b"1", b"2", b"3", b"4", b"5"):
                requested = "main" if key == b"1" else key.decode("ascii")
                if any(row["profile"] == requested for row in rows):
                    return requested
    except KeyboardInterrupt:
        return None
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
        output_stream.write("\x1b[?25h\x1b[?1049l")
        output_stream.flush()


def print_rows(rows, active=None):
    for row in rows:
        marker = "*" if active == (row["agent"], row["profile"]) else " "
        auth = _row_state(row)
        main = " (main)" if row.get("main") else ""
        print(
            f"{marker} {row['agent']}/{row['profile']}  {row['label']}{main}  "
            f"[{row['isolation']}, {auth}]"
        )
        if row["authDetail"]:
            print(f"    {row['authDetail']}")
        for line in row["live"]:
            print(f"    {line}")
        if row["agent"] == "claude" and not row["live"]:
            print("    limits: use /usage inside Claude (no standalone usage API)")


def run_setup(store, config, cwd):
    rows = profile_rows(store, config)
    print("Super Codex setup")
    print(f"Config: {store.config_path}")
    print()
    print_rows(rows, active=store.selection(config, cwd)[:2])
    print("\nNext steps:")
    row_map = {(row["agent"], row["profile"]): row for row in rows}
    steps = []
    if not executable("codex"):
        steps.append("Install the Codex CLI, then rerun `sc setup`.")
    elif not row_map[("codex", "main")]["authenticated"]:
        steps.append("Authenticate the existing Codex profile: `sc login --profile main`.")
    if executable("codex"):
        extra_codex = [
            row for row in rows if row["agent"] == "codex" and row["profile"] != "main"
        ]
        if not extra_codex:
            steps.append("Optional: add account 2 with `sc profile add codex 2 --label Personal`.")
        for row in extra_codex:
            if not row["authenticated"]:
                steps.append(
                    f"Authenticate Codex account {row['profile']}: "
                    f"`sc login --profile {row['profile']}`."
                )
    if not executable("claude"):
        steps.append("Optional: install Claude Code to enable the fallback agent.")
    else:
        claude_profile = config["agentDefaults"]["claude"]
        claude_row = row_map.get(("claude", claude_profile))
        if claude_row is None or not claude_row["authenticated"]:
            steps.append(
                "Authenticate Claude: "
                f"`sc login --agent claude --profile {claude_profile}`."
            )
    if not steps:
        steps.append("Setup is complete. Run `sc` to start Codex.")
    for index, step in enumerate(steps, 1):
        print(f"{index}. {step}")
    print("\nIntegrated workflow:")
    print("- Each Codex account profile keeps its own session history.")
    print("- Codex can consult Claude through a read-only MCP tool in every `sc` session.")
    print("  Inside Codex, ask: 'Ask Claude to review this change.'")
    print("\nUse `sc status --live` whenever you need verified Codex limits.")
    return 0


def provider_rows(store, config, cwd):
    active, matched = store.provider_selection(config, cwd)
    rows = [
        {
            "provider": DEFAULT_PROVIDER,
            "label": "Account authentication (built in)",
            "baseUrl": "",
            "envKey": "",
            "model": "",
            "active": active == DEFAULT_PROVIDER,
        }
    ]
    for name in sorted(config.get("providers", {})):
        data = config["providers"][name]
        rows.append(
            {
                "provider": name,
                "label": data["label"],
                "baseUrl": data["baseUrl"],
                "envKey": data["envKey"],
                "model": data["model"],
                "active": active == name,
            }
        )
    return rows, active, matched


def run_provider(store, config, cwd, args):
    if args.provider_command == "list":
        rows, active, matched = provider_rows(store, config, cwd)
        if args.json:
            print(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "active": active,
                        "binding": matched,
                        "providers": rows,
                    },
                    indent=2,
                )
            )
            return 0
        for row in rows:
            marker = "*" if row["active"] else " "
            print(f"{marker} {row['provider']}  {row['label']}")
            if row["baseUrl"]:
                print(f"    {row['baseUrl']}  model {row['model']}")
                state = "set" if os.environ.get(row["envKey"]) else "NOT SET"
                print(f"    credential: ${row['envKey']} [{state}]")
        if len(rows) == 1:
            print(
                "\nAdd one with: sc provider add <name> --base-url <url> "
                "--env-key <VARIABLE> --model <slug>"
            )
        return 0
    if args.provider_command == "add":
        store.add_provider(
            config,
            args.name,
            base_url=args.base_url,
            env_key=args.env_key,
            model=args.model,
            label=args.label,
            wire_api=args.wire_api,
            reasoning=args.reasoning,
        )
        print(f"Added provider {args.name}.")
        if not os.environ.get(args.env_key):
            print(
                f"Export its credential before launching: "
                f'export {args.env_key}="..." (the `export` matters; a bare '
                "assignment is invisible to the agent process)"
            )
        print(f"Use it with: sc --provider {args.name}")
        return 0
    if args.provider_command == "remove":
        _, unbound = store.remove_provider(config, args.name)
        print(f"Removed provider {args.name}.")
        for workspace in unbound:
            print(f"Workspace returned to {DEFAULT_PROVIDER}: {workspace}")
        return 0
    if args.provider_command == "use":
        store.bind_provider(config, cwd, args.name, args.globally)
        scope = "global default" if args.globally else cwd
        if args.name == DEFAULT_PROVIDER:
            print(f"Routing {scope} through account authentication")
            return 0
        print(f"Routing {scope} through provider {args.name}")
        if args.name != DEFAULT_PROVIDER:
            env_key = config["providers"][args.name]["envKey"]
            if not os.environ.get(env_key):
                print(f"Warning: ${env_key} is not exported in this shell.")
        return 0
    if args.provider_command == "show":
        if args.name == DEFAULT_PROVIDER:
            print(
                f"{DEFAULT_PROVIDER} uses the account's own authentication and "
                "generates no Codex profile."
            )
            return 0
        data = store.require_provider(config, args.name)
        print(f"# {store.codex_profile_name(args.name)} (written into the active CODEX_HOME)")
        print(render_codex_provider_profile(args.name, data), end="")
        return 0
    raise ConfigError(f"Unsupported provider command: {args.provider_command}")


def reconcile_session_names(store, config, profile=None, env=None):
    if "sessions" not in store.session_sharing_groups(config):
        return 0
    base_env = dict(env or os.environ)
    shared_home = store.shared_codex_home(base_env)
    rows = store.codex_homes(config, base_env)
    if profile:
        profile = store.normalize_profile("codex", profile)
        store.require_profile(config, "codex", profile)
        rows = [row for row in rows if row["profile"] == profile]
    targets = [
        row for row in rows if row["isolated"] and row["mode"] == "shared"
    ]
    if not targets:
        return 0
    homes = [shared_home]
    for row in targets:
        if row["home"] not in homes:
            homes.append(row["home"])
    metadata = {}
    environments = {}
    for home in homes:
        process_env = base_env.copy()
        process_env["CODEX_HOME"] = str(home)
        environments[home] = process_env
        metadata[home] = codex_thread_names(process_env)
    canonical = {}
    for home in homes:
        tie_breaker = 1 if home == shared_home else 0
        for thread_id, thread in metadata[home].items():
            name = thread.get("name")
            if not name:
                continue
            rank = (thread.get("updatedAt", 0), tie_breaker)
            if thread_id not in canonical or rank > canonical[thread_id][1]:
                canonical[thread_id] = (name, rank)
    applied = 0
    for home in homes:
        updates = {
            thread_id: name
            for thread_id, (name, _) in canonical.items()
            if thread_id in metadata[home]
            and metadata[home][thread_id].get("name") != name
        }
        applied += codex_set_thread_names(environments[home], updates)
    return applied


def run_sessions(store, config, args):
    command = args.sessions_command
    if command == "status":
        rows = store.sessions_status(config)
        sharing = store.session_sharing(config)
        if args.json:
            print(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "default": sharing.get("default"),
                        "include": store.session_sharing_groups(config),
                        "since": sharing.get("since"),
                        "store": str(rows[0]["store"]) if rows else None,
                        "accounts": [
                            {
                                "profile": row["profile"],
                                "label": row["label"],
                                "mode": row["mode"],
                                "home": str(row["home"]),
                                "rollouts": row["rollouts"],
                                "pending": row["pending"],
                            }
                            for row in rows
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        if rows:
            print(f"Store:    {rows[0]['store']}")
        print(f"Sharing:  {sharing.get('default')} by default")
        print(f"Includes: {', '.join(store.session_sharing_groups(config)) or 'nothing'}")
        print()
        pending = 0
        for row in rows:
            note = "store" if not row["isolated"] else row["mode"]
            print(f"  {row['profile']:<6} {row['label']:<18} {note}")
            detail = f"{row['rollouts']} transcripts"
            if row["pending"]:
                detail += f", {row['pending']} not unified"
                pending += row["pending"]
            print(f"         {detail}")
        if pending:
            print(
                f"\n{pending} transcripts predate sharing. Preview with: "
                "sc sessions merge --dry-run"
            )
        return 0
    if command == "merge":
        reports = store.merge_sessions(
            config, profile=args.profile, dry_run=args.dry_run
        )
        total = sum(report["pulled"] + report["pushed"] for report in reports)
        for report in reports:
            moved = report["pulled"] + report["pushed"]
            if moved:
                print(f"  {report['profile']}: {moved} transcripts")
        if args.dry_run:
            print(f"Would unify {total} transcripts. Nothing was written.")
            return 0
        names = reconcile_session_names(store, config, args.profile)
        configs = sum(len(report["config"]) for report in reports)
        print(f"Unified {total} transcripts.")
        print(f"Synchronized {names} session names and {configs} configuration files.")
        if total:
            print(
                "They cost no extra disk space: each is one file with a name in "
                "every account."
            )
        return 0
    if command in ("share", "split"):
        mode = "shared" if command == "share" else "isolated"
        store.set_session_sharing(config, mode, args.profile)
        scope = f"profile {args.profile}" if args.profile else "every account"
        if mode == "shared":
            print(f"Sharing session history for {scope}.")
            print("Run sc sessions merge to unify transcripts recorded before now.")
        else:
            print(f"Keeping new sessions private to {scope}.")
            print(
                "Transcripts already shared stay visible; unlinking them would "
                "risk removing the only copy."
            )
        return 0
    if command == "sync":
        reports = store.reconcile_sessions(config, profile=args.profile)
        total = sum(report["pulled"] + report["pushed"] for report in reports)
        names = reconcile_session_names(store, config, args.profile)
        configs = sum(len(report["config"]) for report in reports)
        print(
            f"Reconciled {total} transcripts, synchronized {names} session names, "
            f"and refreshed {configs} configuration files."
        )
        return 0
    if command in ("include", "exclude"):
        groups = store.set_session_groups(
            config, args.groups, include=command == "include"
        )
        print(f"Shared with every account: {', '.join(groups) or 'nothing'}")
        return 0
    if command == "purge":
        candidates, cutoff = store.purge_candidates(config, args.older_than)
        if not candidates:
            print(
                f"No Codex transcripts older than {args.older_than} days "
                f"(before {cutoff:%Y-%m-%d %H:%M:%S})."
            )
            return 0
        categories = {"sessions": 0, "archived_sessions": 0}
        unique_transcripts = {}
        for candidate in candidates:
            categories[candidate["directory"]] += 1
            unique_transcripts[(candidate["device"], candidate["inode"])] = candidate[
                "size"
            ]
        print(
            f"Would permanently delete {len(candidates)} transcript paths "
            f"({len(unique_transcripts)} unique transcripts) older than {args.older_than} days "
            f"(before {cutoff:%Y-%m-%d %H:%M:%S})."
        )
        print(f"  sessions:          {categories['sessions']}")
        print(f"  archived_sessions: {categories['archived_sessions']}")
        reclaimable = sum(unique_transcripts.values())
        print(f"  Reclaimable storage: {format_byte_count(reclaimable)}")
        roots = sorted({candidate["root"] for candidate in candidates}, key=os.fspath)
        print("\nAffected locations (matching rollout files only):")
        for root in roots:
            print(f"  {root}/*")
        if args.dry_run:
            print("\nNothing was deleted.")
            return 0
        try:
            confirmation = input("\nType PURGE to permanently delete these transcripts: ")
        except (EOFError, KeyboardInterrupt):
            print("\nPurge cancelled. Nothing was deleted.")
            return 130
        if confirmation != "PURGE":
            print("Purge cancelled. Nothing was deleted.")
            return 0
        deleted = store.purge_transcripts(candidates)
        print(
            f"Permanently deleted {deleted} transcript paths "
            f"({len(unique_transcripts)} unique transcripts)."
        )
        return 0
    raise ConfigError(f"Unsupported sessions command: {command}")


def format_byte_count(value):
    """Render an exact byte count with a compact binary-unit estimate."""
    if value < 1024:
        return f"{value} bytes"
    units = ("KiB", "MiB", "GiB", "TiB")
    estimate = float(value)
    for unit in units:
        estimate /= 1024
        if estimate < 1024 or unit == units[-1]:
            return f"{estimate:.1f} {unit} ({value:,} bytes)"


def run_bindings(config, agent_filter=None, json_output=False):
    rows = [
        {"workspace": workspace, "agent": binding["agent"], "profile": binding["profile"]}
        for workspace, binding in sorted(config.get("workspaces", {}).items())
        if not agent_filter or binding["agent"] == agent_filter
    ]
    if json_output:
        print(json.dumps({"schemaVersion": 1, "bindings": rows}, indent=2))
    elif not rows:
        print("No workspace bindings configured.")
    else:
        for row in rows:
            print(f"{row['workspace']} -> {row['agent']}/{row['profile']}")
    return 0


def adopt_foreign_session(store, config, env, session):
    """Make a session recorded in another account resumable from this one.

    Codex resolves a session id inside `CODEX_HOME` only, which is why resuming
    used to require the account that recorded it. When the id is not in the home
    about to run, find the transcript in another account and hard link it in
    first. Filenames are all that gets inspected.
    """
    home = env.get("CODEX_HOME")
    if not home:
        return None
    if store.find_rollout_in(home, session):
        return None
    found = store.find_rollout(config, session, env=env, skip=home)
    if not found:
        return None
    adopted = store.adopt_rollout(home, found)
    if adopted:
        print(f"Adopted {found.name} from another account.")
    return adopted


def session_sharing_note(store, config, agent, profile):
    """One line describing where this account records its sessions."""
    if agent != "codex":
        return "Claude keeps its own session history"
    mode = store.session_sharing_mode(config, profile)
    if mode != "shared":
        return f"private to codex/{profile} (sc sessions share to unify)"
    if store.session_sharing(config).get("since"):
        return "shared; transcripts from before sharing await sc sessions merge"
    return "shared with every account and with a bare codex"


def post_session_sync(store, config, agent, profile):
    if agent != "codex" or store.session_sharing_mode(config, profile) != "shared":
        return None
    groups = set(store.session_sharing_groups(config))
    if not groups.intersection(
        {"sessions", "archived_sessions", "attachments", "history"}
    ):
        return None
    targets = store.codex_homes(config)
    if not any(row["isolated"] and row["mode"] == "shared" for row in targets):
        return None

    def synchronize():
        store.reconcile_sessions(config)
        reconcile_session_names(store, config)

    return synchronize


def run_status(store, config, cwd, live=False, json_output=False):
    agent, profile, matched = store.selection(config, cwd)
    provider, provider_matched = store.provider_selection(config, cwd)
    rows = profile_rows(store, config, live=live)
    if json_output:
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "workspace": cwd,
                    "startupMode": config["startupMode"],
                    "active": {
                        "agent": agent,
                        "profile": profile,
                        "provider": provider,
                    },
                    "binding": matched,
                    "providerBinding": provider_matched,
                    "sessionSharing": {
                        "mode": store.session_sharing_mode(config, profile)
                        if agent == "codex"
                        else None,
                        "default": store.session_sharing(config).get("default"),
                        "since": store.session_sharing(config).get("since"),
                    },
                    "profiles": rows,
                },
                indent=2,
            )
        )
        return 0
    print("Super Codex")
    print(f"Workspace: {cwd}")
    print(f"Active:    {agent}/{profile}")
    print(f"Binding:   {matched or 'global default'}")
    print(f"Bare sc:   {config['startupMode']}")
    provider_note = (
        "account authentication"
        if provider == DEFAULT_PROVIDER
        else config["providers"][provider]["baseUrl"]
    )
    print(f"Provider:  {provider} ({provider_note})")
    print(f"Sessions:  {session_sharing_note(store, config, agent, profile)}")
    print("Priority:  Codex primary; Claude available inside Codex as a read-only consultant")
    print()
    print_rows(rows, active=(agent, profile))
    if not live:
        print("\nRun `sc status --live` for verified Codex usage windows.")
    return 0


def run_usage(store, config, cwd, check_all):
    if check_all:
        targets = {
            ("codex", name) for name in config["profiles"]["codex"]
        }
        active = None
    else:
        agent, profile, _ = store.selection(config, cwd)
        if agent != "codex":
            profile = config["agentDefaults"]["codex"]
            print(f"Claude is active; showing Codex usage for codex/{profile}.")
        targets = {("codex", profile)}
        active = ("codex", profile)
    print_rows(profile_rows(store, config, live=True, only=targets), active=active)
    return 0


def run_doctor(store, config, cwd, live):
    failures = 0
    print(f"Config: {store.config_path}")
    for agent in AGENTS:
        path = executable(agent)
        if path:
            print(f"OK   {agent}: {path} ({version(agent)})")
        else:
            failures += 1
            print(f"FAIL {agent}: not installed")
    try:
        store.validate(config)
        print("OK   config: valid schema")
    except ConfigError as exc:
        failures += 1
        print(f"FAIL config: {exc}")
    agent, profile, matched = store.selection(config, cwd)
    print(f"OK   selection: {agent}/{profile} ({matched or 'global'})")
    rows = profile_rows(store, config, live=live)
    for row in rows:
        if row.get("error"):
            failures += 1
            state = "FAIL"
        else:
            state = "OK" if row["authenticated"] else "WARN"
        print(f"{state:<4} {row['agent']}/{row['profile']}: {row['authDetail']}")
        for line in row["live"]:
            print(f"     {line}")
    return 1 if failures else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "upd":
        argv[0] = "update"
    bare_invocation = not argv
    main_shorthand = bool(argv and argv[0] == "main")
    if main_shorthand:
        argv = ["start"] + argv[1:]
    elif argv and argv[0] in set(CODEX_PROFILE_NAMES) | {"1"}:
        argv = ["start", "--profile", argv[0]] + argv[1:]
    if bare_invocation:
        argv = ["start"]
    args = parser().parse_args(argv)
    if args.command == "version":
        print_installation_provenance(args.json)
        return 0
    if args.command == "update":
        try:
            return run_update(args.check, args.version)
        except ReleaseError as exc:
            print(f"sc: {exc}", file=sys.stderr)
            return 2
    if args.command == "uninstall":
        try:
            return run_uninstall()
        except ReleaseError as exc:
            print(f"sc: {exc}", file=sys.stderr)
            return 2
    store = Store()
    cwd = str(Path.cwd().resolve())
    try:
        if args.command == "mcp-server":
            return serve_mcp(store, cwd)
        config = store.load()
        if args.command == "setup":
            return run_setup(store, config, cwd)
        if args.command in ("start", "ask", "resume"):
            if main_shorthand and not (args.agent or args.profile):
                agent, profile = "codex", config["agentDefaults"]["codex"]
            elif bare_invocation:
                agent, profile, _ = store.selection(config, cwd)
            if (
                bare_invocation
                and config["startupMode"] == "select"
                and agent == "codex"
            ):
                targets = {
                    ("codex", name) for name in store.ordered_profile_names(config, "codex")
                }
                try:
                    profile = choose_profile(
                        profile_rows(store, config, live=True, only=targets),
                        initial_profile=profile,
                    )
                except KeyboardInterrupt:
                    profile = None
                if profile is None:
                    print("Account selection cancelled.")
                    return 130
            elif not bare_invocation and (
                not main_shorthand or args.agent or args.profile
            ):
                agent, profile, _ = selected(store, config, args, cwd)
            env = store.environment(agent, profile, config)
            provider, _ = store.provider_selection(
                config, cwd, getattr(args, "provider", None)
            )
            if provider != DEFAULT_PROVIDER:
                if agent != "codex":
                    raise ConfigError(
                        f"Providers apply to Codex only; {agent} uses its own "
                        "account authentication"
                    )
                if not args.dry_run:
                    store.materialize_codex_provider(env, config, provider)
            if (
                args.command == "resume"
                and agent == "codex"
                and getattr(args, "session", None)
                and not args.dry_run
            ):
                adopt_foreign_session(store, config, env, args.session)
            command = build_command(
                agent,
                args.command,
                cwd,
                prompt=getattr(args, "prompt", None),
                session=getattr(args, "session", None),
                use_last=getattr(args, "last", False),
                model=args.model,
                reasoning=args.reasoning,
                native=args.native,
                mcp_command=launcher_command() if agent == "codex" else None,
                provider=None if provider == DEFAULT_PROVIDER else provider,
            )
            after = (
                None
                if args.dry_run
                else post_session_sync(store, config, agent, profile)
            )
            return exec_command(
                command, env, cwd, args.dry_run, agent, after=after
            )
        if args.command == "status":
            return run_status(store, config, cwd, args.live, args.json)
        if args.command == "usage":
            return run_usage(store, config, cwd, args.all)
        if args.command == "use":
            store.bind(config, cwd, args.agent, args.profile, args.globally)
            scope = "global default" if args.globally else cwd
            print(f"Using {args.agent}/{args.profile} for {scope}")
            return 0
        if args.command == "unuse":
            if store.unbind(config, cwd):
                print(f"Removed workspace binding for {cwd}")
            else:
                print(f"No exact workspace binding exists for {cwd}")
            return 0
        if args.command == "bindings":
            return run_bindings(config, args.agent, args.json)
        if args.command == "profiles":
            rows = profile_rows(store, config)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print_rows(rows)
            return 0
        if args.command == "provider":
            return run_provider(store, config, cwd, args)
        if args.command == "sessions":
            return run_sessions(store, config, args)
        if args.command == "profile" and args.profile_command == "add":
            name = store.normalize_profile(args.agent, args.name)
            existing = config["profiles"][args.agent].get(name)
            if existing:
                if args.label is not None:
                    display_label = args.label.strip()
                    if not display_label or len(display_label) > 80:
                        raise ConfigError("Profile labels must contain 1-80 characters")
                print(
                    f"Profile {args.agent}/{name} already exists. It will be overridden "
                    "only after authentication succeeds; cancel login to keep it unchanged."
                )
                provider_home, env = store.replacement_environment(
                    args.agent, name, config
                )
                command = auth_command(args.agent, "login")
                result = run_command(command, env, cwd)
                if result == 0:
                    authentication = auth_status(args.agent, env)
                    if not authentication.authenticated:
                        print(
                            "Native login exited without a verified authentication; "
                            f"{authentication.detail}."
                        )
                        result = 1
                if result != 0:
                    store.discard_provider_home(config, args.agent, provider_home)
                    print(f"Kept existing profile {args.agent}/{name} unchanged.")
                    return result
                try:
                    previous_home = store.commit_profile_replacement(
                        config, args.agent, name, provider_home, args.label
                    )
                except Exception:
                    store.discard_provider_home(config, args.agent, provider_home)
                    raise
                print(f"Overrode {args.agent}/{name} after successful authentication.")
                print(
                    "Retained the previous provider home so running sessions are not "
                    f"disrupted: {previous_home}"
                )
                return 0
            store.add_profile(config, args.agent, args.name, args.label, args.shared)
            isolation = "shared" if args.shared else "isolated"
            print(f"Added {args.agent}/{name} ({isolation})")
            if args.agent == "claude" and not args.shared:
                print("Note: isolated Claude profiles rely on the undocumented CLAUDE_CONFIG_DIR interface.")
            env = store.environment(args.agent, name, config)
            command = auth_command(args.agent, "login")
            return run_command(command, env, cwd)
        if args.command == "profile" and args.profile_command == "label":
            store.set_label(config, args.agent, args.name, args.label)
            print(f"Labeled {args.agent}/{args.name} as {args.label.strip()}")
            return 0
        if args.command == "profile" and args.profile_command == "main":
            profile = store.normalize_profile(args.agent, args.name)
            store.set_main_profile(config, args.agent, profile)
            print(f"Main {args.agent.capitalize()} account: {args.agent}/{profile}")
            return 0
        if args.command == "profile" and args.profile_command == "order":
            store.set_profile_order(config, args.agent, args.names)
            print(f"{args.agent.capitalize()} profile order: {' '.join(args.names)}")
            return 0
        if args.command == "login":
            agent, profile, _ = selected(store, config, args, cwd)
            env = store.environment(agent, profile, config)
            command = auth_command(agent, "login")
            if args.dry_run:
                print(command_display(command, env, agent))
                return 0
            return exec_command(command, env, cwd, False, agent)
        if args.command == "doctor":
            return run_doctor(store, config, cwd, args.live)
        if args.command == "config":
            if args.action != "mode" and args.value is not None:
                raise ConfigError(f"`sc config {args.action}` does not accept a value")
            if args.action == "path":
                print(store.config_path)
            elif args.action == "show":
                print(json.dumps(config, indent=2, sort_keys=True))
            else:
                if args.value is not None:
                    mode = "select" if args.value == "picker" else args.value
                    store.set_startup_mode(config, mode)
                    config = store.load()
                print(config["startupMode"])
            return 0
        parser().print_help()
        return 0
    except (ConfigError, AdapterError) as exc:
        print(f"sc: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
