import fcntl
import hashlib
import json
import math
import os
import re
import selectors
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
STATUS_TOOL_NAME = "claude_job_status"
CANCEL_TOOL_NAME = "cancel_claude_job"
MAX_OUTPUT_CHARS = 8_000
MAX_DIFF_CHARS = 24_000
DEFAULT_TIMEOUT_SECONDS = 90
MAX_TIMEOUT_SECONDS = 1_800
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
DEFAULT_EFFORT = "low"
DEFAULT_EXECUTION_PROFILE = "standard"
EXECUTION_PROFILES = {
    "quick": {"timeout_seconds": 60, "max_turns": 3},
    "standard": {"timeout_seconds": 300, "max_turns": 10},
    "deep": {"timeout_seconds": 1_800, "max_turns": 30},
}
MAX_TURNS = 50
AUTO_DETACH_SECONDS = 10
MAX_STATUS_WAIT_SECONDS = 30
COMPLETED_JOB_RETENTION_SECONDS = 15 * 60
MAX_RETAINED_JOBS = 20
POLL_INTERVAL_SECONDS = 0.2
TERMINATION_GRACE_SECONDS = 2
WORKER_SHUTDOWN_GRACE_SECONDS = 5


def _monotonic_timestamp():
    return time.monotonic()


class ClaudeJob:
    def __init__(
        self,
        job_id,
        profile,
        execution_profile,
        timeout_seconds,
        max_turns,
        max_budget_usd,
    ):
        self.job_id = job_id
        self.profile = profile
        self.execution_profile = execution_profile
        self.timeout_seconds = timeout_seconds
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.created_at = _monotonic_timestamp()
        self.started_at = None
        self.completed_at = None
        self.last_activity_at = None
        self.activity_count = 0
        self.turns = None
        self.reported_cost_usd = None
        self.state = "queued"
        self.result = None
        self.error = None
        self.cancel_event = threading.Event()
        self.condition = threading.Condition()
        self.worker = None

    def update_activity(self, event):
        with self.condition:
            self.last_activity_at = _monotonic_timestamp()
            self.activity_count += 1
            turns = event.get("num_turns")
            if isinstance(turns, int) and not isinstance(turns, bool):
                self.turns = turns
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                self.reported_cost_usd = float(cost)
            self.condition.notify_all()

    def finish(self, state, result=None, error=None):
        with self.condition:
            self.state = state
            self.result = result
            self.error = error
            self.completed_at = _monotonic_timestamp()
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            now = _monotonic_timestamp()
            started_at = self.started_at or self.created_at
            elapsed = max(0.0, (self.completed_at or now) - started_at)
            remaining = (
                0.0
                if self.completed_at is not None
                else max(0.0, self.timeout_seconds - elapsed)
            )
            data = {
                "job_id": self.job_id,
                "state": self.state,
                "profile": self.profile,
                "execution_profile": self.execution_profile,
                "elapsed_seconds": round(elapsed, 1),
                "remaining_seconds": round(remaining, 1),
                "timeout_seconds": self.timeout_seconds,
                "max_turns": self.max_turns,
                "activity_count": self.activity_count,
            }
            if self.max_budget_usd is not None:
                data["max_budget_usd"] = self.max_budget_usd
            if self.turns is not None:
                data["turns"] = self.turns
            if self.reported_cost_usd is not None:
                data["reported_cost_usd"] = self.reported_cost_usd
            if self.last_activity_at is not None:
                data["seconds_since_activity"] = round(
                    max(0.0, now - self.last_activity_at), 1
                )
            if self.result is not None:
                data["result"] = self.result
            if self.error is not None:
                data["error"] = self.error
            return data


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
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise AdapterError(f"{name} must be between {minimum} and {maximum}")
    return value


def _budget_value(raw, name="max_budget_usd"):
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not 0.01 <= value <= 10:
        raise AdapterError(f"{name} must be between 0.01 and 10")
    return value


def _budget_setting(env):
    return _budget_value(
        env.get("SUPER_CODEX_CLAUDE_MAX_BUDGET_USD"),
        "SUPER_CODEX_CLAUDE_MAX_BUDGET_USD",
    )


def _bounded_integer(value, name, minimum, maximum):
    if not isinstance(value, int) or isinstance(value, bool):
        raise AdapterError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise AdapterError(f"{name} must be between {minimum} and {maximum}")
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


def _append_bounded(current, addition, limit=MAX_OUTPUT_CHARS):
    combined = current + addition
    return combined[-limit:] if len(combined) > limit else combined


def _stream_claude_output(
    process,
    input_text,
    deadline,
    cancel_event=None,
    progress_callback=None,
):
    selector = selectors.DefaultSelector()
    buffers = {"stdout": b"", "stderr": b""}
    stderr_tail = ""
    plain_stdout = ""
    final_output = None

    def consume_line(channel, raw_line):
        nonlocal stderr_tail, plain_stdout, final_output
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            return
        if channel == "stderr":
            stderr_tail = _append_bounded(stderr_tail, line + "\n")
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            plain_stdout = _append_bounded(plain_stdout, line + "\n")
            return
        if not isinstance(event, dict):
            return
        if progress_callback is not None:
            progress_callback(event)
        if event.get("type") == "result":
            result = event.get("result")
            if isinstance(result, str):
                final_output = result

    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        process.stdin.write(input_text.encode("utf-8"))
        process.stdin.close()
        process.stdin = None
        while selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                raise AdapterError("Claude consultation cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError("Claude consultation timed out")
            events = selector.select(min(POLL_INTERVAL_SECONDS, remaining))
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 8_192)
                except OSError:
                    chunk = b""
                channel = key.data
                if not chunk:
                    selector.unregister(key.fileobj)
                    pending = buffers[channel]
                    if pending:
                        consume_line(channel, pending)
                    key.fileobj.close()
                    continue
                pending = buffers[channel] + chunk
                lines = pending.split(b"\n")
                buffers[channel] = lines.pop()
                for raw_line in lines:
                    consume_line(channel, raw_line)
        process.wait()
    finally:
        selector.close()
    output = final_output if final_output is not None else plain_stdout.strip()
    return output, stderr_tail.strip()


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
    max_turns=None,
    max_budget_usd=None,
    profile=None,
    config=None,
    progress_callback=None,
):
    path = executable("claude")
    if not path:
        raise AdapterError("claude is not installed or not on PATH")
    config = config or store.load()
    profile = profile or config["agentDefaults"]["claude"]
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
    if max_turns is None:
        configured_turns = env.get("SUPER_CODEX_CLAUDE_MAX_TURNS")
        if configured_turns is not None:
            max_turns = _integer_setting(
                env,
                "SUPER_CODEX_CLAUDE_MAX_TURNS",
                None,
                1,
                MAX_TURNS,
            )
    else:
        max_turns = _bounded_integer(max_turns, "max_turns", 1, MAX_TURNS)
    if max_budget_usd is None:
        max_budget_usd = _budget_setting(env)
    else:
        max_budget_usd = _budget_value(max_budget_usd)
    effort = _effort_setting(env)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)
    env["CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"] = "1"
    command = [
        path,
        "-p",
        "--output-format",
        "stream-json" if progress_callback is not None else "text",
        "--tools",
        "Read,Glob,Grep",
        "--effort",
        effort,
    ]
    if progress_callback is not None:
        command.append("--verbose")
    if max_turns is not None:
        command.extend(["--max-turns", str(max_turns)])
    if max_budget_usd is not None:
        command.extend(["--max-budget-usd", f"{max_budget_usd:g}"])
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
                text=progress_callback is None,
                start_new_session=True,
            )
            deadline = time.monotonic() + effective_timeout
            input_text = _consultation_prompt(
                prompt, change_context, continuing=session_id is not None
            )
            if progress_callback is not None:
                stdout, stderr = _stream_claude_output(
                    process,
                    input_text,
                    deadline,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
            else:
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


class ClaudeJobManager:
    def __init__(
        self,
        store,
        cwd,
        consult=run_claude_consult,
        session_registry=None,
        detach_seconds=AUTO_DETACH_SECONDS,
    ):
        self.store = store
        self.cwd = str(Path(cwd).resolve())
        self.consult = consult
        self.session_registry = session_registry or ClaudeSessionRegistry()
        self.detach_seconds = detach_seconds
        self._lock = threading.Lock()
        self._jobs = {}
        self._active = {}

    def _cleanup_locked(self):
        now = _monotonic_timestamp()
        completed = [
            job
            for job in self._jobs.values()
            if job.completed_at is not None
        ]
        expired = {
            job.job_id
            for job in completed
            if now - job.completed_at >= COMPLETED_JOB_RETENTION_SECONDS
        }
        retained = sorted(completed, key=lambda job: job.completed_at, reverse=True)
        expired.update(job.job_id for job in retained[MAX_RETAINED_JOBS:])
        for job_id in expired:
            self._jobs.pop(job_id, None)

    def _job(self, job_id):
        if not isinstance(job_id, str) or not job_id:
            raise AdapterError("job_id must be a non-empty string")
        with self._lock:
            self._cleanup_locked()
            job = self._jobs.get(job_id)
        if job is None:
            raise AdapterError("Unknown or expired Claude job")
        return job

    @staticmethod
    def _limits(arguments, env):
        execution_profile = arguments.get(
            "execution_profile", DEFAULT_EXECUTION_PROFILE
        )
        if execution_profile not in EXECUTION_PROFILES:
            available = ", ".join(EXECUTION_PROFILES)
            raise AdapterError(f"execution_profile must be one of: {available}")
        defaults = EXECUTION_PROFILES[execution_profile]
        timeout_seconds = arguments.get(
            "timeout_seconds", defaults["timeout_seconds"]
        )
        timeout_seconds = _bounded_integer(
            timeout_seconds, "timeout_seconds", 10, MAX_TIMEOUT_SECONDS
        )
        if "timeout_seconds" not in arguments:
            configured_timeout = env.get("SUPER_CODEX_CLAUDE_TIMEOUT_SECONDS")
            if configured_timeout is not None:
                timeout_seconds = _integer_setting(
                    env,
                    "SUPER_CODEX_CLAUDE_TIMEOUT_SECONDS",
                    timeout_seconds,
                    10,
                    MAX_TIMEOUT_SECONDS,
                )
        max_turns = arguments.get("max_turns", defaults["max_turns"])
        max_turns = _bounded_integer(max_turns, "max_turns", 1, MAX_TURNS)
        if "max_turns" not in arguments:
            configured_turns = env.get("SUPER_CODEX_CLAUDE_MAX_TURNS")
            if configured_turns is not None:
                max_turns = _integer_setting(
                    env,
                    "SUPER_CODEX_CLAUDE_MAX_TURNS",
                    max_turns,
                    1,
                    MAX_TURNS,
                )
        if "max_budget_usd" in arguments:
            max_budget_usd = _budget_value(arguments["max_budget_usd"])
        else:
            max_budget_usd = _budget_setting(env)
        return execution_profile, timeout_seconds, max_turns, max_budget_usd

    def start(self, prompt, arguments, request_cancel_event=None):
        config = self.store.load()
        profile = config["agentDefaults"]["claude"]
        env = self.store.environment("claude", profile, config)
        execution_profile, timeout_seconds, max_turns, max_budget_usd = self._limits(
            arguments, env
        )
        key = (self.cwd, profile)
        job = ClaudeJob(
            str(uuid.uuid4()),
            profile,
            execution_profile,
            timeout_seconds,
            max_turns,
            max_budget_usd,
        )
        with self._lock:
            self._cleanup_locked()
            active_job_id = self._active.get(key)
            if active_job_id is not None:
                active = self._jobs.get(active_job_id)
                if active is not None and active.state in ("queued", "running"):
                    raise AdapterError(
                        "A Claude consultation is already active for this workspace and "
                        "profile; monitor or cancel that job instead of retrying"
                    )
            self._jobs[job.job_id] = job
            self._active[key] = job.job_id

        options = {
            "model": arguments.get("model"),
            "timeout": timeout_seconds,
            "cancel_event": job.cancel_event,
            "include_diff": arguments.get("include_diff", False),
            "new_context": arguments.get("new_context", False),
            "session_registry": self.session_registry,
            "max_turns": max_turns,
            "max_budget_usd": max_budget_usd,
            "profile": profile,
            "config": config,
            "progress_callback": job.update_activity,
        }

        def run():
            try:
                result = self.consult(self.store, self.cwd, prompt, **options)
            except AdapterError as exc:
                message = str(exc)
                if "cancelled" in message.lower():
                    state = "cancelled"
                elif "timed out" in message.lower():
                    state = "timed_out"
                else:
                    state = "failed"
                job.finish(state, error=message)
            except Exception:
                job.finish("failed", error="Claude consultation failed unexpectedly")
            else:
                job.finish("completed", result=result)
            finally:
                with self._lock:
                    if self._active.get(key) == job.job_id:
                        self._active.pop(key, None)

        worker = threading.Thread(
            target=run,
            name=f"claude-job-{job.job_id}",
            daemon=True,
        )
        job.worker = worker
        with job.condition:
            job.state = "running"
            job.started_at = _monotonic_timestamp()
            job.last_activity_at = job.started_at
        try:
            worker.start()
        except RuntimeError as exc:
            with self._lock:
                self._jobs.pop(job.job_id, None)
                if self._active.get(key) == job.job_id:
                    self._active.pop(key, None)
            raise AdapterError(f"Cannot start Claude consultation worker: {exc}") from exc

        if arguments.get("background") is True:
            return job.snapshot()
        deadline = _monotonic_timestamp() + self.detach_seconds
        with job.condition:
            while job.state in ("queued", "running"):
                if request_cancel_event is not None and request_cancel_event.is_set():
                    job.cancel_event.set()
                    break
                remaining = deadline - _monotonic_timestamp()
                if remaining <= 0:
                    break
                job.condition.wait(min(POLL_INTERVAL_SECONDS, remaining))
        return job.snapshot()

    def status(self, job_id, wait_seconds=0):
        wait_seconds = _bounded_integer(
            wait_seconds, "wait_seconds", 0, MAX_STATUS_WAIT_SECONDS
        )
        job = self._job(job_id)
        if wait_seconds:
            deadline = _monotonic_timestamp() + wait_seconds
            with job.condition:
                initial_activity = job.activity_count
                while job.state in ("queued", "running"):
                    if job.activity_count != initial_activity:
                        break
                    remaining = deadline - _monotonic_timestamp()
                    if remaining <= 0:
                        break
                    job.condition.wait(remaining)
        return job.snapshot()

    def cancel(self, job_id):
        job = self._job(job_id)
        job.cancel_event.set()
        return job.snapshot()

    def shutdown(self):
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.state in ("queued", "running"):
                job.cancel_event.set()
        deadline = _monotonic_timestamp() + WORKER_SHUTDOWN_GRACE_SECONDS
        for job in jobs:
            worker = job.worker
            if worker is None or not worker.is_alive():
                continue
            remaining = deadline - _monotonic_timestamp()
            if remaining <= 0:
                break
            worker.join(remaining)


def tool_definition():
    return {
        "name": TOOL_NAME,
        "description": (
            "Start exactly one managed, read-only Claude consultation. Fast results return "
            "directly; work exceeding ten seconds continues as a background job. Pass the "
            "user's request directly. Use quick or deep only when the user explicitly asks "
            "for that execution depth, and set max_budget_usd only when the user explicitly "
            "specifies a dollar limit. Never retry a running or failed start automatically."
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
                "execution_profile": {
                    "type": "string",
                    "enum": list(EXECUTION_PROFILES),
                    "description": (
                        "Execution envelope. Defaults to standard; choose quick or deep "
                        "only from explicit user wording."
                    ),
                    "default": DEFAULT_EXECUTION_PROFILE,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": MAX_TIMEOUT_SECONDS,
                    "description": "Explicit wall-clock override for this consultation.",
                },
                "max_turns": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TURNS,
                    "description": "Explicit Claude agent-turn override for this consultation.",
                },
                "max_budget_usd": {
                    "type": "number",
                    "minimum": 0.01,
                    "maximum": 10,
                    "description": (
                        "Optional Claude --max-budget-usd limit. Omit unless the user "
                        "explicitly specifies a dollar budget."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Return the job immediately instead of waiting ten seconds for a "
                        "fast result."
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


def status_tool_definition():
    return {
        "name": STATUS_TOOL_NAME,
        "description": (
            "Read local status for an existing Claude job. This never starts Claude or "
            "consumes additional Claude usage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "minLength": 1},
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_STATUS_WAIT_SECONDS,
                    "default": 0,
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Claude Job Status",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


def cancel_tool_definition():
    return {
        "name": CANCEL_TOOL_NAME,
        "description": "Cancel an existing Claude job and terminate its process group.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "minLength": 1}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Cancel Claude Job",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
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
    job_manager=None,
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
                    "For an explicit request to ask or use Claude, call ask_claude exactly "
                    "once with the user's text. Fast results return directly; otherwise poll "
                    "the returned job with claude_job_status. Never start a replacement job "
                    "automatically. Use quick or deep only when explicitly requested, and "
                    "set max_budget_usd only when the user explicitly gives a dollar limit. "
                    "Set new_context=true only for an explicit fresh-context request. Claude "
                    "is read-only and advisory; Codex remains responsible for decisions."
                ),
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(
            request_id,
            {
                "tools": [
                    tool_definition(),
                    status_tool_definition(),
                    cancel_tool_definition(),
                ]
            },
        )
    if method == "tools/call":
        params = message.get("params") or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments must be an object")
        if tool_name == STATUS_TOOL_NAME:
            if job_manager is None:
                return _error(request_id, -32603, "Claude job manager is unavailable")
            try:
                snapshot = job_manager.status(
                    arguments.get("job_id"), arguments.get("wait_seconds", 0)
                )
            except AdapterError as exc:
                return _result(
                    request_id,
                    {"content": [{"type": "text", "text": str(exc)}], "isError": True},
                )
            return _result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(snapshot, sort_keys=True)}
                    ],
                    "isError": False,
                },
            )
        if tool_name == CANCEL_TOOL_NAME:
            if job_manager is None:
                return _error(request_id, -32603, "Claude job manager is unavailable")
            try:
                snapshot = job_manager.cancel(arguments.get("job_id"))
            except AdapterError as exc:
                return _result(
                    request_id,
                    {"content": [{"type": "text", "text": str(exc)}], "isError": True},
                )
            return _result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(snapshot, sort_keys=True)}
                    ],
                    "isError": False,
                },
            )
        if tool_name not in (TOOL_NAME, LEGACY_TOOL_NAME):
            return _error(request_id, -32602, "Unknown tool")
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
        execution_profile = arguments.get("execution_profile")
        if execution_profile is not None and execution_profile not in EXECUTION_PROFILES:
            return _error(request_id, -32602, "invalid execution_profile")
        for name, minimum, maximum in (
            ("timeout_seconds", 10, MAX_TIMEOUT_SECONDS),
            ("max_turns", 1, MAX_TURNS),
        ):
            value = arguments.get(name)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                return _error(
                    request_id,
                    -32602,
                    f"{name} must be an integer between {minimum} and {maximum}",
                )
        max_budget_usd = arguments.get("max_budget_usd")
        if max_budget_usd is not None and (
            not isinstance(max_budget_usd, (int, float))
            or isinstance(max_budget_usd, bool)
            or not math.isfinite(float(max_budget_usd))
            or not 0.01 <= float(max_budget_usd) <= 10
        ):
            return _error(
                request_id,
                -32602,
                "max_budget_usd must be a number between 0.01 and 10",
            )
        background = arguments.get("background")
        if background is not None and not isinstance(background, bool):
            return _error(request_id, -32602, "background must be a boolean")
        try:
            effective_arguments = dict(arguments)
            effective_arguments.pop("prompt", None)
            effective_arguments.pop("request", None)
            if include_diff is True or (
                include_diff is None and _requests_change_context(prompt)
            ):
                effective_arguments["include_diff"] = True
            if job_manager is not None:
                snapshot = job_manager.start(
                    prompt,
                    effective_arguments,
                    request_cancel_event=cancel_event,
                )
                state = snapshot["state"]
                if state == "completed":
                    text = snapshot.get("result") or "Claude returned no text."
                    is_error = False
                elif state in ("failed", "timed_out", "cancelled"):
                    text = json.dumps(snapshot, sort_keys=True)
                    is_error = True
                else:
                    text = json.dumps(snapshot, sort_keys=True)
                    is_error = False
                return _result(
                    request_id,
                    {"content": [{"type": "text", "text": text}], "isError": is_error},
                )
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
            if "timeout_seconds" in arguments:
                options["timeout"] = arguments["timeout_seconds"]
            if "max_turns" in arguments:
                options["max_turns"] = arguments["max_turns"]
            if "max_budget_usd" in arguments:
                options["max_budget_usd"] = arguments["max_budget_usd"]
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
    job_manager = (
        ClaudeJobManager(
            store,
            cwd,
            consult=consult,
            session_registry=session_registry,
        )
        if consult is run_claude_consult
        else None
    )

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
                job_manager=job_manager,
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
                    job_manager=job_manager,
                )
            )
    finally:
        cancel_pending()
        if job_manager is not None:
            job_manager.shutdown()
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
