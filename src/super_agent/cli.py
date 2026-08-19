import argparse
import json
import sys
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
    version,
)
from .config import AGENTS, ConfigError, Store


def parser():
    root = argparse.ArgumentParser(
        prog="sc",
        description="A Codex-first account and workspace control plane for coding agents.",
        epilog="Examples: sc setup | sc status --live | sc use codex second | sc start --agent claude",
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command")

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
    add = profile_commands.add_parser("add", help="Add a profile without copying credentials")
    add.add_argument("agent", choices=AGENTS)
    add.add_argument("name")
    add.add_argument("--label")
    add.add_argument("--shared", action="store_true", help="Inherit the provider's normal home")
    label = profile_commands.add_parser("label", help="Change a profile's display label")
    label.add_argument("agent", choices=AGENTS)
    label.add_argument("name")
    label.add_argument("label")

    login = commands.add_parser("login", help="Run the provider's native login flow")
    add_selection_arguments(login)
    login.add_argument("--dry-run", action="store_true")

    doctor = commands.add_parser("doctor", help="Check local installation and state")
    doctor.add_argument("--live", action="store_true")

    config = commands.add_parser("config", help="Show the source-of-truth configuration")
    config.add_argument("action", choices=("path", "show"), nargs="?", default="path")
    return root


def add_selection_arguments(command):
    command.add_argument("--agent", choices=AGENTS)
    command.add_argument("--profile")


def add_launch_arguments(command, prompt_required):
    command.add_argument("prompt", nargs=None if prompt_required else "?")
    add_selection_arguments(command)
    command.add_argument("--model")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--native", nargs=argparse.REMAINDER, default=[])


def selected(store, config, args, cwd):
    return store.selection(
        config,
        cwd,
        agent=getattr(args, "agent", None),
        profile=getattr(args, "profile", None),
    )


def profile_rows(store, config, live=False, only=None):
    rows = []
    for agent in AGENTS:
        for name, data in config["profiles"][agent].items():
            if only and (agent, name) not in only:
                continue
            env = store.environment(agent, name, config)
            status = auth_status(agent, env)
            row = {
                "agent": agent,
                "profile": name,
                "label": data.get("label", name),
                "isolation": data["isolation"],
                "authenticated": status.authenticated,
                "authDetail": status.detail,
                "live": [],
            }
            if live and agent == "codex" and status.authenticated:
                try:
                    sqlite_home = store.home / "runtime" / "codex" / name
                    row["live"] = format_codex_live(codex_live_status(env, sqlite_home=sqlite_home))
                except AdapterError as exc:
                    row["live"] = [f"usage unavailable: {exc}"]
            rows.append(row)
    return rows


def print_rows(rows, active=None):
    for row in rows:
        marker = "*" if active == (row["agent"], row["profile"]) else " "
        auth = "ready" if row["authenticated"] else "login needed"
        print(
            f"{marker} {row['agent']}/{row['profile']}  {row['label']}  "
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
    if executable("codex") and not row_map[("codex", "second")]["authenticated"]:
        steps.append("Authenticate Codex account 2: `sc login --profile second`.")
    if not executable("claude"):
        steps.append("Optional: install Claude Code to enable the fallback agent.")
    elif not row_map[("claude", "main")]["authenticated"]:
        steps.append("Authenticate Claude: `sc login --agent claude --profile main`.")
    if not steps:
        steps.append("Setup is complete. Run `sc` to start Codex.")
    for index, step in enumerate(steps, 1):
        print(f"{index}. {step}")
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
    print("Priority:  Codex accounts, then Claude by explicit choice")
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
        state = "OK" if row["authenticated"] else "WARN"
        print(f"{state:<4} {row['agent']}/{row['profile']}: {row['authDetail']}")
        for line in row["live"]:
            print(f"     {line}")
    return 1 if failures else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["start"]
    args = parser().parse_args(argv)
    store = Store()
    cwd = str(Path.cwd().resolve())
    try:
        config = store.load()
        if args.command == "setup":
            return run_setup(store, config, cwd)
        if args.command in ("start", "ask", "resume"):
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
                native=args.native,
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
            store.add_profile(config, args.agent, args.name, args.label, args.shared)
            isolation = "shared" if args.shared else "isolated"
            print(f"Added {args.agent}/{args.name} ({isolation})")
            if args.agent == "claude" and not args.shared:
                print("Note: isolated Claude profiles rely on the undocumented CLAUDE_CONFIG_DIR interface.")
            print(f"Next: sc login --agent {args.agent} --profile {args.name}")
            return 0
        if args.command == "profile" and args.profile_command == "label":
            store.set_label(config, args.agent, args.name, args.label)
            print(f"Labeled {args.agent}/{args.name} as {args.label.strip()}")
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
            if args.action == "path":
                print(store.config_path)
            else:
                print(json.dumps(config, indent=2, sort_keys=True))
            return 0
        parser().print_help()
        return 0
    except (ConfigError, AdapterError) as exc:
        print(f"sc: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
