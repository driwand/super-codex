import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .adapters import AdapterError, executable
from .config import ensure_private_directory

SERVER_NAME = "super-codex-claude"
TOOL_NAME = "ask_claude"
LEGACY_TOOL_NAME = "consult_claude"
MAX_OUTPUT_CHARS = 8_000
MAX_DIFF_CHARS = 24_000
DEFAULT_TIMEOUT_SECONDS = 90
MAX_TIMEOUT_SECONDS = 165
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_EFFORT = "low"
POLL_INTERVAL_SECONDS = 0.2
TERMINATION_GRACE_SECONDS = 2
WORKER_SHUTDOWN_GRACE_SECONDS = 5


class ClaudeSessionRegistry:
    """Process-local Claude session IDs, isolated by workspace and profile."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}

    @staticmethod
    def _key(cwd, profile):
        return str(Path(cwd).resolve()), profile

    def get(self, cwd, profile):
        with self._lock:
            return self._sessions.get(self._key(cwd, profile))

    def replace(self, cwd, profile, session_id):
        with self._lock:
            self._sessions[self._key(cwd, profile)] = session_id

    def discard(self, cwd, profile, session_id=None):
        with self._lock:
            key = self._key(cwd, profile)
            if session_id is None or self._sessions.get(key) == session_id:
                self._sessions.pop(key, None)


def _integer_setting(env, name, default, minimum, maximum):
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise AdapterError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise AdapterError(f"{name} must be between {minimum} and {maximum}")
    return value


def _budget_setting(env):
    raw = env.get("SUPER_CODEX_CLAUDE_MAX_BUDGET_USD")
    if raw is None:
        return DEFAULT_MAX_BUDGET_USD
    try:
        value = float(raw)
    except ValueError as exc:
        raise AdapterError("SUPER_CODEX_CLAUDE_MAX_BUDGET_USD must be a number") from exc
    if not math.isfinite(value) or not 0.01 <= value <= 10:
        raise AdapterError(
            "SUPER_CODEX_CLAUDE_MAX_BUDGET_USD must be between 0.01 and 10"
        )
    return value


def _effort_setting(env):
    value = env.get("SUPER_CODEX_CLAUDE_EFFORT", DEFAULT_EFFORT).lower()
    if value not in ("low", "medium", "high", "xhigh", "max"):
        raise AdapterError(
            "SUPER_CODEX_CLAUDE_EFFORT must be low, medium, high, xhigh, or max"
        )
    return value


def _consultation_prompt(prompt, change_context=None, continuing=False):
    if continuing:
        text = prompt.strip()
    else:
        text = (
            "Act as a read-only consultant to Codex. Answer the request directly and "
            "concisely. Lead with concrete findings and include only the evidence, "
            "material caveats, and next actions needed. For a broad review, prioritize "
            "at most five high-impact findings instead of exhaustively inventorying the "
            "repository. Do not explain this integration or modify files.\n\nRequest:\n"
            + prompt.strip()
        )
    if change_context:
        text += (
            "\n\nBounded read-only Git change context supplied by Super Codex:\n"
            + change_context
        )
    return text


def _requests_change_context(prompt):
    return bool(
        re.search(
            r"\b(change|changes|changed|diff|staged|uncommitted|worktree)\b",
            prompt,
            re.IGNORECASE,
        )
    )


def _current_change_context(cwd, env):
    git = executable("git")
    if not git:
        return "Git is unavailable; inspect only workspace files relevant to the request."
    git_env = env.copy()
    git_env["GIT_OPTIONAL_LOCKS"] = "0"
    git_env["GIT_PAGER"] = "cat"
    commands = (
        ("Status", [git, "status", "--short", "--untracked-files=all"]),
        ("Unstaged diff", [git, "diff", "--no-ext-diff", "--no-textconv", "--"]),
        (
            "Staged diff",
            [git, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--"],
        ),
    )
    sections = []
    for title, command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=git_env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip():
            sections.append(f"## {title}\n{result.stdout.strip()}")
    if not sections:
        return "Git reported no readable staged, unstaged, or untracked change summary."
    context = "\n\n".join(sections)
    if len(context) <= MAX_DIFF_CHARS:
        return context
    marker = "\n\n[Git change context truncated by Super Codex.]"
    return context[: MAX_DIFF_CHARS - len(marker)].rstrip() + marker


def _bounded_output(output):
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    marker = "\n\n[Claude output truncated by Super Codex; ask for a narrower review.]"
    return output[: MAX_OUTPUT_CHARS - len(marker)].rstrip() + marker


@contextmanager
def consultation_lock(store, cwd, profile):
    runtime = Path(store.home) / "runtime"
    directory = runtime / "claude-consultations"
    ensure_private_directory(store.home)
    ensure_private_directory(runtime, parents=False)
    ensure_private_directory(directory, parents=False)
    identity = f"{Path(cwd).resolve()}\0{profile}".encode("utf-8")
    lock_path = directory / (hashlib.sha256(identity).hexdigest() + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(lock_path), flags, 0o600)
    except OSError as exc:
        raise AdapterError(f"Cannot open Claude consultation lock: {exc}") from exc
    acquired = False
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid():
            raise AdapterError("Refusing unsafe Claude consultation lock")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AdapterError(
                "A Claude consultation is already active for this workspace and profile; "
                "wait for it to finish instead of retrying"
            ) from exc
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _terminate_process_group(process):
    if process.poll() is not None:
        return
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            break
        except OSError:
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    try:
        process.communicate(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.communicate()


def run_claude_consult(
    store,
    cwd,
    prompt,
    model=None,
    timeout=None,
    cancel_event=None,
    include_diff=False,
    new_context=False,
    session_registry=None,
):
    path = executable("claude")
    if not path:
        raise AdapterError("claude is not installed or not on PATH")
    config = store.load()
    profile = config["agentDefaults"]["claude"]
    env = store.environment("claude", profile, config)
    effective_timeout = timeout
    if effective_timeout is None:
        effective_timeout = _integer_setting(
            env,
            "SUPER_CODEX_CLAUDE_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
            10,
            MAX_TIMEOUT_SECONDS,
        )
    max_output_tokens = _integer_setting(
        env,
        "SUPER_CODEX_CLAUDE_MAX_OUTPUT_TOKENS",
        DEFAULT_MAX_OUTPUT_TOKENS,
        512,
        16_384,
    )
    max_budget_usd = _budget_setting(env)
    effort = _effort_setting(env)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)
    env["CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"] = "1"
    command = [
        path,
        "-p",
        "--output-format",
        "text",
        "--tools",
        "Read,Glob,Grep",
        "--effort",
        effort,
        "--max-budget-usd",
        f"{max_budget_usd:g}",
    ]
    if model:
        command.extend(["--model", model])
    process = None
    stdout = ""
    stderr = ""
    with consultation_lock(store, cwd, profile):
        session_id = None
        if session_registry is not None and not new_context:
            session_id = session_registry.get(cwd, profile)
        invocation = list(command)
        if session_id:
            invocation.extend(["--resume", session_id])
            active_session_id = session_id
        else:
            active_session_id = str(uuid.uuid4())
            invocation.extend(["--session-id", active_session_id])
        try:
            change_context = _current_change_context(cwd, env) if include_diff else None
            process = subprocess.Popen(
                invocation,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                text=True,
                start_new_session=True,
            )
            deadline = time.monotonic() + effective_timeout
            input_text = _consultation_prompt(
                prompt, change_context, continuing=session_id is not None
            )
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise AdapterError("Claude consultation cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterError("Claude consultation timed out")
                try:
                    stdout, stderr = process.communicate(
                        input=input_text,
                        timeout=min(POLL_INTERVAL_SECONDS, remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    input_text = None
        except OSError as exc:
            raise AdapterError(f"Cannot run Claude consultation: {exc}") from exc
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_group(process)
        output = stdout.strip()
        if process.returncode != 0:
            if session_registry is not None and session_id is not None:
                session_registry.discard(cwd, profile, session_id)
            detail = (stderr or stdout).strip().splitlines()
            raise AdapterError(detail[-1] if detail else "Claude consultation failed")
        if session_registry is not None:
            session_registry.replace(cwd, profile, active_session_id)
        return _bounded_output(output) if output else "Claude returned no text."


def tool_definition():
    return {
        "name": TOOL_NAME,
        "description": (
            "Directly ask the locally authenticated Claude Code CLI for a concise, "
            "read-only workspace analysis. Call this immediately when the user says "
            "'ask Claude', requests a Claude review, second opinion, or cross-model check. "
            "Pass the user's request directly; Claude can inspect the workspace itself. "
            "Do not inspect this integration or invoke it through the shell first. Never "
            "retry while a prior consultation may still be active."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": (
                        "A question, follow-up, or review task for Claude. Follow-ups may "
                        "refer to earlier Claude responses in this Codex session."
                    ),
                    "minLength": 1,
                },
                "model": {
                    "type": "string",
                    "description": "Optional Claude model alias or full model name.",
                    "minLength": 1,
                },
                "include_diff": {
                    "type": "boolean",
                    "description": (
                        "Attach a bounded, read-only Git status and staged/unstaged diff. "
                        "Use for requests about current changes; omission auto-detects "
                            "change-related wording in the request."
                    ),
                },
                "new_context": {
                    "type": "boolean",
                    "description": (
                        "Start a fresh Claude conversation for this workspace and profile. "
                        "Set only when the user explicitly asks for fresh context."
                    ),
                    "default": False,
                },
            },
            "required": ["request"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Ask Claude",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    }


def _result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_request(
    message,
    store,
    cwd,
    consult=run_claude_consult,
    cancel_event=None,
    session_registry=None,
):
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    if method == "initialize":
        params = message.get("params") or {}
        protocol = params.get("protocolVersion") or "2025-06-18"
        return _result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "For an explicit request to ask or use Claude, call ask_claude directly "
                    "with the user's text in the request field. Follow-up calls continue "
                    "Claude's context for "
                    "this Codex session. Set new_context=true only when the user explicitly "
                    "asks for fresh context. Do not inspect Super Codex or invoke its MCP "
                    "server through the shell. Never retry while a prior consultation may "
                    "still be active. Claude is read-only and advisory; Codex remains "
                    "responsible for decisions and edits."
                ),
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": [tool_definition()]})
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") not in (TOOL_NAME, LEGACY_TOOL_NAME):
            return _error(request_id, -32602, "Unknown tool")
        arguments = params.get("arguments") or {}
        prompt = arguments.get("request")
        legacy_prompt = arguments.get("prompt")
        model = arguments.get("model")
        include_diff = arguments.get("include_diff")
        new_context = arguments.get("new_context")
        if prompt is None:
            prompt = legacy_prompt
        elif legacy_prompt is not None and legacy_prompt != prompt:
            return _error(
                request_id,
                -32602,
                "request and legacy prompt fields must not conflict",
            )
        if not isinstance(prompt, str) or not prompt.strip():
            return _error(request_id, -32602, "request must be a non-empty string")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            return _error(request_id, -32602, "model must be a non-empty string")
        if include_diff is not None and not isinstance(include_diff, bool):
            return _error(request_id, -32602, "include_diff must be a boolean")
        if new_context is not None and not isinstance(new_context, bool):
            return _error(request_id, -32602, "new_context must be a boolean")
        try:
            options = {"model": model}
            if include_diff is True or (
                include_diff is None and _requests_change_context(prompt)
            ):
                options["include_diff"] = True
            if new_context is True:
                options["new_context"] = True
            if cancel_event is not None:
                options["cancel_event"] = cancel_event
            if session_registry is not None and consult is run_claude_consult:
                options["session_registry"] = session_registry
            text = consult(store, cwd, prompt, **options)
        except AdapterError as exc:
            return _result(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        return _result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )
    return _error(request_id, -32601, f"Method not found: {method}")


def serve(
    store,
    cwd=None,
    input_stream=None,
    output_stream=None,
    consult=run_claude_consult,
):
    cwd = str(Path(cwd or Path.cwd()).resolve())
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    output_lock = threading.Lock()
    pending_lock = threading.Lock()
    pending = {}
    workers = set()
    session_registry = ClaudeSessionRegistry()

    def write_response(response):
        if response is not None:
            try:
                with output_lock:
                    output_stream.write(
                        json.dumps(response, separators=(",", ":")) + "\n"
                    )
                    output_stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def cancel_pending():
        with pending_lock:
            events = list(pending.values())
        for event in events:
            event.set()

    def run_tool(message, request_id, cancel_event):
        try:
            response = handle_request(
                message,
                store,
                cwd,
                consult=consult,
                cancel_event=cancel_event,
                session_registry=session_registry,
            )
        except Exception:
            response = _error(request_id, -32603, "Claude consultation failed unexpectedly")
        finally:
            with pending_lock:
                if pending.get(request_id) is cancel_event:
                    pending.pop(request_id, None)
                workers.discard(threading.current_thread())
        write_response(response)

    previous_handlers = {}

    def stop_on_signal(signum, frame):
        cancel_pending()
        raise SystemExit(128 + signum)

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop_on_signal)
    try:
        for line in input_stream:
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("request must be an object")
            except (json.JSONDecodeError, ValueError) as exc:
                write_response(_error(None, -32700, f"Parse error: {exc}"))
                continue

            method = message.get("method")
            if method == "notifications/cancelled":
                request_id = (message.get("params") or {}).get("requestId")
                with pending_lock:
                    event = pending.get(request_id)
                if event is not None:
                    event.set()
                continue
            if method == "tools/call" and message.get("id") is not None:
                request_id = message["id"]
                if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                    write_response(_error(None, -32600, "Invalid request id"))
                    continue
                cancel_event = threading.Event()
                with pending_lock:
                    if request_id in pending:
                        write_response(_error(request_id, -32600, "Duplicate request id"))
                        continue
                    pending[request_id] = cancel_event
                worker = threading.Thread(
                    target=run_tool,
                    args=(message, request_id, cancel_event),
                    name=f"claude-consult-{request_id}",
                    daemon=True,
                )
                with pending_lock:
                    workers.add(worker)
                worker.start()
                continue
            write_response(
                handle_request(
                    message,
                    store,
                    cwd,
                    consult=consult,
                    session_registry=session_registry,
                )
            )
    finally:
        cancel_pending()
        deadline = time.monotonic() + WORKER_SHUTDOWN_GRACE_SECONDS
        with pending_lock:
            remaining_workers = list(workers)
        for worker in remaining_workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    return 0
