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
    codex_live_status,
    command_display,
    exec_command,
    executable,
    format_codex_live,
    run_command,
    version,
)
from .config import AGENTS, CODEX_PROFILE_NAMES, ConfigError, Store
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
            row["live"] = format_codex_live(codex_live_status(env, sqlite_home=sqlite_home))
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


def _picker_lines(rows, selected_index):
    lines = [
        "Choose a Codex account",
        "Use ↑/↓ to move, Enter to select, 1-5 for quick select, or q to cancel.",
        "",
    ]
    for index, row in enumerate(rows):
        marker = ">" if index == selected_index else " "
        if row.get("error"):
            state = "unavailable"
        else:
            state = "ready" if row["authenticated"] else "login needed"
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
    selected_index = next(
        (
            index
            for index, row in enumerate(rows)
            if row["profile"] == initial_profile
        ),
        0,
    )
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
        if row.get("error"):
            auth = "unavailable"
        else:
            auth = "ready" if row["authenticated"] else "login needed"
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


def run_status(store, config, cwd, live=False, json_output=False):
    agent, profile, matched = store.selection(config, cwd)
    rows = profile_rows(store, config, live=live)
    if json_output:
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "workspace": cwd,
                    "startupMode": config["startupMode"],
                    "active": {"agent": agent, "profile": profile},
                    "binding": matched,
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
            )
            return exec_command(command, env, cwd, args.dry_run, agent)
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
