import datetime
import json
import os
import selectors
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__

CLAUDE_ROUTING_INSTRUCTIONS = (
    "When the user asks to use, ask, consult, or have Claude review anything, call the "
    "super_codex_claude.ask_claude MCP tool immediately and exactly once. Put the "
    "user's request directly in the tool's request field. For current changes, diffs, "
    "staged, or uncommitted work, set "
    "include_diff=true. Follow-up calls continue Claude's context for this Codex session. "
    "Set new_context=true only when the user explicitly requests a fresh Claude context. "
    "Do not inspect files or Git first. Never invoke claude, sc ask "
    "--agent claude, super-codex mcp-server, or MCP JSON through the shell. If the MCP "
    "tool is unavailable, already active, fails, or is cancelled, report that and stop; "
    "never retry automatically. Treat Claude output as advisory."
)
from .config import ConfigError, ensure_private_directory


@dataclass
class AuthStatus:
    authenticated: bool
    detail: str


@dataclass
class LiveStatus:
    account: dict
    rate_limits: dict


class AdapterError(RuntimeError):
    pass


def executable(agent):
    return shutil.which(agent)


def version(agent):
    path = executable(agent)
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=4, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    value = (result.stdout or result.stderr).strip().splitlines()
    return value[-1] if value else "unknown"


def auth_status(agent, env):
    if not executable(agent):
        return AuthStatus(False, "not installed")
    command = [agent, "login", "status"] if agent == "codex" else [agent, "auth", "status", "--json"]
    try:
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=8, check=False
        )
    except subprocess.TimeoutExpired:
        return AuthStatus(False, "status timed out")
    except OSError as exc:
        return AuthStatus(False, str(exc))
    if agent == "codex":
        text = (result.stdout or result.stderr).strip()
        detail = text.splitlines()[-1] if text else "not logged in"
        return AuthStatus(result.returncode == 0, detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        text = (result.stdout or result.stderr).strip()
        return AuthStatus(result.returncode == 0, text.splitlines()[-1] if text else "unknown")
    logged_in = bool(payload.get("loggedIn"))
    identity = payload.get("email") or payload.get("subscriptionType") or payload.get("authMethod")
    provider = payload.get("apiProvider")
    detail = " / ".join(str(value) for value in (identity, provider) if value)
    return AuthStatus(logged_in, detail or ("logged in" if logged_in else "not logged in"))


def _app_server_requests(env, messages, timeout):
    try:
        process = subprocess.Popen(
            ["codex", "app-server", "--stdio"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise AdapterError(str(exc)) from exc
    if not process.stdin or not process.stdout or not process.stderr:
        process.terminate()
        raise AdapterError("Could not open Codex app-server pipes")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    responses = {}
    errors = []
    sent_after_initialize = False
    expected_ids = {message["id"] for message in messages if "id" in message}
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    try:
        send(messages[0])
        while expected_ids - responses.keys():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append("Codex usage request timed out")
                break
            events = selector.select(remaining)
            if not events:
                if process.poll() is not None:
                    break
                continue
            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
                    continue
                if key.data == "stderr":
                    errors.append(line.strip())
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message_id = message.get("id")
                if message_id in expected_ids:
                    responses[message_id] = message
                if message_id == 1 and not sent_after_initialize:
                    if message.get("error"):
                        break
                    for pending in messages[1:]:
                        send(pending)
                    sent_after_initialize = True
            initialization_error = responses.get(1, {}).get("error")
            if initialization_error:
                errors.append(initialization_error.get("message", "Codex app-server initialization failed"))
                break
            if process.poll() is not None and not selector.get_map():
                break
    except OSError as exc:
        raise AdapterError(f"Codex app-server communication failed: {exc}") from exc
    finally:
        selector.close()
        try:
            process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    return responses, [error for error in errors if error], process.returncode


def codex_live_status(env, timeout=12, sqlite_home=None):
    if not executable("codex"):
        raise AdapterError("codex is not installed")
    process_env = env.copy()
    if sqlite_home:
        sqlite_path = Path(sqlite_home)
        try:
            ensure_private_directory(sqlite_path)
        except ConfigError as exc:
            raise AdapterError(str(exc)) from exc
        process_env["CODEX_SQLITE_HOME"] = str(sqlite_path.absolute())
    messages = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "super-codex",
                    "title": "Super Codex",
                    "version": __version__,
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized"},
        {"id": 2, "method": "account/read", "params": {"refreshToken": False}},
        {"id": 3, "method": "account/rateLimits/read"},
    ]
    responses, errors, return_code = _app_server_requests(process_env, messages, timeout)
    account_message = responses.get(2, {})
    limits_message = responses.get(3, {})
    error = limits_message.get("error") or account_message.get("error")
    if error:
        raise AdapterError(error.get("message", "Codex app-server request failed"))
    if 3 not in responses:
        reason = errors[-1] if errors else f"Codex app-server exited with {return_code}"
        raise AdapterError(reason)
    account_result = account_message.get("result") or {}
    return LiveStatus(
        account=account_result.get("account") or {},
        rate_limits=limits_message.get("result") or {},
    )


def codex_claude_mcp_arguments(command):
    return [
        "-c",
        f"mcp_servers.super_codex_claude.command={json.dumps(command)}",
        "-c",
        'mcp_servers.super_codex_claude.args=["mcp-server"]',
        "-c",
        "mcp_servers.super_codex_claude.required=true",
        "-c",
        'mcp_servers.super_codex_claude.enabled_tools=["ask_claude"]',
        "-c",
        "mcp_servers.super_codex_claude.tool_timeout_sec=180",
        "-c",
        'mcp_servers.super_codex_claude.tools.ask_claude.approval_mode="auto"',
        "-c",
        f"developer_instructions={json.dumps(CLAUDE_ROUTING_INSTRUCTIONS)}",
    ]


def build_command(
    agent,
    action,
    cwd,
    prompt=None,
    session=None,
    use_last=False,
    model=None,
    reasoning=None,
    native=None,
    mcp_command=None,
):
    native = list(native or [])
    if agent == "codex":
        if action == "start":
            command = ["codex", "-C", cwd]
        elif action == "ask":
            command = ["codex", "exec", "-C", cwd, "--skip-git-repo-check"]
        elif action == "resume":
            command = ["codex", "resume", "-C", cwd]
            if use_last:
                command.append("--last")
        else:
            raise AdapterError(f"Unsupported action: {action}")
        if mcp_command:
            command.extend(codex_claude_mcp_arguments(mcp_command))
        if model:
            command.extend(["--model", model])
        if reasoning:
            command.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning)}"])
        command.extend(native)
        if action == "resume" and session:
            command.append(session)
        if prompt:
            command.append(prompt)
        return command
    if agent == "claude":
        if action == "start":
            command = ["claude"]
        elif action == "ask":
            command = ["claude", "-p"]
        elif action == "resume":
            if session:
                command = ["claude", "-r", session]
            elif use_last:
                command = ["claude", "-c"]
            else:
                command = ["claude", "-r"]
        else:
            raise AdapterError(f"Unsupported action: {action}")
        if model:
            command.extend(["--model", model])
        command.extend(native)
        if prompt:
            command.append(prompt)
        return command
    raise AdapterError(f"Unsupported agent: {agent}")


def auth_command(agent, operation):
    if operation not in ("login", "status"):
        raise AdapterError(f"Unsupported auth operation: {operation}")
    if agent == "codex":
        return ["codex", "login"] if operation == "login" else ["codex", "login", "status"]
    if agent == "claude":
        return ["claude", "auth", operation]
    raise AdapterError(f"Unsupported agent: {agent}")


def command_display(command, env, agent):
    prefix = ""
    variable = "CODEX_HOME" if agent == "codex" else "CLAUDE_CONFIG_DIR"
    if variable in env and env.get(variable) != os.environ.get(variable):
        prefix = f"{variable}={shlex.quote(env[variable])} "
    return prefix + " ".join(shlex.quote(value) for value in command)


def format_reset(timestamp):
    if not timestamp:
        return "unknown reset"
    value = datetime.datetime.fromtimestamp(timestamp).astimezone()
    return value.strftime("%a %H:%M")


def format_window(window):
    if not window:
        return None
    minutes = window.get("windowDurationMins")
    if minutes and minutes % 1440 == 0:
        name = f"{minutes // 1440}d"
    elif minutes and minutes % 60 == 0:
        name = f"{minutes // 60}h"
    elif minutes:
        name = f"{minutes}m"
    else:
        name = "window"
    return f"{name}: {window.get('usedPercent', '?')}% used, resets {format_reset(window.get('resetsAt'))}"


def format_amount(value):
    try:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def format_codex_live(status):
    account = status.account
    identity = account.get("email") or account.get("type") or "authenticated"
    plan = account.get("planType")
    lines = [f"account: {identity}" + (f" ({plan})" if plan else "")]
    payload = status.rate_limits
    snapshots = payload.get("rateLimitsByLimitId") or {}
    if not snapshots and payload.get("rateLimits"):
        snapshots = {"codex": payload["rateLimits"]}
    for limit_id, snapshot in snapshots.items():
        label = snapshot.get("limitName") or limit_id
        details = [format_window(snapshot.get("primary")), format_window(snapshot.get("secondary"))]
        details = [detail for detail in details if detail]
        individual = snapshot.get("individualLimit")
        if individual:
            details.append(
                "spend: "
                f"{individual.get('remainingPercent', '?')}% remaining "
                f"({format_amount(individual.get('used'))}/{format_amount(individual.get('limit'))}), "
                f"resets {format_reset(individual.get('resetsAt'))}"
            )
        if snapshot.get("spendControlReached"):
            details.append("spend limit reached")
        if snapshot.get("rateLimitReachedType"):
            details.append(f"limit state: {snapshot['rateLimitReachedType']}")
        credits = snapshot.get("credits") or {}
        if credits.get("unlimited"):
            details.append("credits: unlimited")
        elif credits.get("balance") is not None:
            details.append(f"credits: {credits['balance']}")
        lines.append(f"{label}: " + ("; ".join(details) if details else "no reported limits"))
    reset_credits = payload.get("rateLimitResetCredits") or {}
    if reset_credits.get("availableCount"):
        lines.append(f"reset credits available: {reset_credits['availableCount']}")
    if len(lines) == 1:
        lines.append("usage: unavailable")
    return lines


def exec_command(command, env, cwd, dry_run=False, agent=None):
    if dry_run:
        print(command_display(command, env, agent))
        return 0
    if not executable(command[0]):
        raise AdapterError(f"{command[0]} is not installed or not on PATH")
    try:
        os.chdir(cwd)
        os.execvpe(command[0], command, env)
    except OSError as exc:
        raise AdapterError(f"Cannot launch {command[0]} in {cwd}: {exc}") from exc
    return 0


def run_command(command, env, cwd):
    """Run an interactive command and wait for its exit status."""
    if not executable(command[0]):
        raise AdapterError(f"{command[0]} is not installed or not on PATH")
    try:
        result = subprocess.run(command, env=env, cwd=cwd, check=False)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise AdapterError(f"Cannot launch {command[0]} in {cwd}: {exc}") from exc
    return 128 - result.returncode if result.returncode < 0 else result.returncode
