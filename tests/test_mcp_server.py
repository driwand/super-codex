import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from super_agent.adapters import AdapterError
from super_agent.mcp_server import (
    ClaudeSessionRegistry,
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_DIFF_CHARS,
    MAX_OUTPUT_CHARS,
    TOOL_NAME,
    _current_change_context,
    consultation_lock,
    handle_request,
    run_claude_consult,
    serve,
)

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"


class McpProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()

    def test_initialize_and_tool_listing(self):
        initialized = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            self.store,
            "/repo",
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        listed = handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            self.store,
            "/repo",
        )
        tool = listed["result"]["tools"][0]
        self.assertEqual(tool["name"], TOOL_NAME)
        self.assertEqual(tool["name"], "ask_claude")
        self.assertEqual(tool["inputSchema"]["required"], ["request"])
        self.assertIn("request", tool["inputSchema"]["properties"])
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertFalse(tool["annotations"]["destructiveHint"])

    def test_tool_call_returns_claude_as_advisory_text(self):
        consult = Mock(return_value="Independent review")
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": TOOL_NAME,
                    "arguments": {"request": "Review this", "model": "sonnet"},
                },
            },
            self.store,
            "/repo",
            consult=consult,
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "Independent review")
        consult.assert_called_once_with(self.store, "/repo", "Review this", model="sonnet")

    def test_legacy_prompt_argument_remains_supported(self):
        consult = Mock(return_value="Independent review")
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {
                    "name": TOOL_NAME,
                    "arguments": {"prompt": "Review this"},
                },
            },
            self.store,
            "/repo",
            consult=consult,
        )
        self.assertFalse(response["result"]["isError"])
        consult.assert_called_once_with(self.store, "/repo", "Review this", model=None)

    def test_conflicting_request_and_legacy_prompt_are_rejected(self):
        consult = Mock()
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 32,
                "method": "tools/call",
                "params": {
                    "name": TOOL_NAME,
                    "arguments": {"request": "One", "prompt": "Two"},
                },
            },
            self.store,
            "/repo",
            consult=consult,
        )
        self.assertIn("must not conflict", response["error"]["message"])
        consult.assert_not_called()

    def test_change_review_automatically_requests_bounded_git_context(self):
        consult = Mock(return_value="Change review")
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": TOOL_NAME,
                    "arguments": {"prompt": "Review these current changes"},
                },
            },
            self.store,
            "/repo",
            consult=consult,
        )
        self.assertFalse(response["result"]["isError"])
        consult.assert_called_once_with(
            self.store,
            "/repo",
            "Review these current changes",
            model=None,
            include_diff=True,
        )

    def test_explicit_fresh_context_is_forwarded(self):
        consult = Mock(return_value="Fresh review")
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": TOOL_NAME,
                    "arguments": {"prompt": "Start over", "new_context": True},
                },
            },
            self.store,
            "/repo",
            consult=consult,
        )
        self.assertFalse(response["result"]["isError"])
        consult.assert_called_once_with(
            self.store,
            "/repo",
            "Start over",
            model=None,
            new_context=True,
        )

    def test_server_ignores_notifications_and_emits_json_lines(self):
        requests = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}),
            ]
        )
        output = io.StringIO()
        serve(self.store, "/repo", io.StringIO(requests), output)
        responses = output.getvalue().splitlines()
        self.assertEqual(len(responses), 1)
        self.assertEqual(json.loads(responses[0])["id"], 7)

    def test_cancel_notification_stops_inflight_consultation(self):
        started = threading.Event()

        def consult(store, cwd, prompt, model=None, cancel_event=None):
            started.set()
            cancel_event.wait(1)
            if cancel_event.is_set():
                raise AdapterError("Claude consultation cancelled")
            return "unexpected"

        requests = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": TOOL_NAME,
                            "arguments": {"prompt": "Review"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 9},
                    }
                ),
            ]
        )
        output = io.StringIO()
        serve(
            self.store,
            "/repo",
            io.StringIO(requests),
            output,
            consult=consult,
        )
        self.assertTrue(started.is_set())
        response = json.loads(output.getvalue())
        self.assertEqual(response["id"], 9)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("cancelled", response["result"]["content"][0]["text"])

    def test_input_eof_cancels_inflight_consultation(self):
        cancelled = threading.Event()

        def consult(store, cwd, prompt, model=None, cancel_event=None):
            cancel_event.wait(1)
            if cancel_event.is_set():
                cancelled.set()
                raise AdapterError("Claude consultation cancelled")
            return "unexpected"

        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": {"prompt": "Review"}},
            }
        )
        serve(
            self.store,
            "/repo",
            io.StringIO(request),
            io.StringIO(),
            consult=consult,
        )
        self.assertTrue(cancelled.is_set())


class ClaudeConsultTests(unittest.TestCase):
    def store(self, directory):
        store = Mock()
        store.home = Path(directory) / "state"
        store.load.return_value = {"agentDefaults": {"claude": "main"}}
        store.environment.return_value = {"PATH": "/bin"}
        return store

    def process(
        self,
        result="Review",
        stderr="",
        returncode=0,
        raw_stdout=None,
    ):
        process = Mock()
        stdout = result if raw_stdout is None else raw_stdout
        process.communicate.return_value = (stdout, stderr)
        process.poll.return_value = returncode
        process.returncode = returncode
        process.pid = 12345
        return process

    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_claude_is_restricted_and_resource_bounded(self, popen, executable):
        with tempfile.TemporaryDirectory() as directory:
            self_store = self.store(directory)
            process = self.process()
            popen.return_value = process
            output = run_claude_consult(self_store, "/repo", "Inspect the change")
        self.assertEqual(output, "Review")
        command = popen.call_args.args[0]
        self.assertEqual(command[0:2], ["/bin/claude", "-p"])
        self.assertIn("Read,Glob,Grep", command)
        self.assertIn("--effort", command)
        self.assertIn("low", command)
        self.assertIn("--max-budget-usd", command)
        self.assertIn(f"{DEFAULT_MAX_BUDGET_USD:g}", command)
        self.assertEqual(command[command.index("--output-format") + 1], "text")
        self.assertNotIn("--no-session-persistence", command)
        self.assertNotIn("--resume", command)
        session_id = command[command.index("--session-id") + 1]
        self.assertEqual(str(uuid.UUID(session_id)), session_id)
        self.assertNotIn("Bash", command)
        self.assertNotIn("Write", command)
        self.assertNotIn("Edit", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(
            child_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],
            str(DEFAULT_MAX_OUTPUT_TOKENS),
        )
        self.assertEqual(child_env["CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"], "1")
        forwarded_prompt = process.communicate.call_args.kwargs["input"]
        self.assertIn("read-only consultant", forwarded_prompt)
        self.assertTrue(forwarded_prompt.endswith("Request:\nInspect the change"))

    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_claude_output_is_bounded_for_codex_context(self, popen, executable):
        with tempfile.TemporaryDirectory() as directory:
            self_store = self.store(directory)
            popen.return_value = self.process(
                result="x" * (MAX_OUTPUT_CHARS + 500)
            )
            output = run_claude_consult(self_store, "/repo", "Inspect")
        self.assertEqual(len(output), MAX_OUTPUT_CHARS)
        self.assertIn("output truncated", output)

    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_follow_up_resumes_the_same_claude_session(self, popen, executable):
        registry = ClaudeSessionRegistry()
        first_process = self.process(result="First")
        second_process = self.process(result="Follow-up")
        popen.side_effect = [first_process, second_process]
        with tempfile.TemporaryDirectory() as directory:
            self_store = self.store(directory)
            first = run_claude_consult(
                self_store, "/repo", "Review", session_registry=registry
            )
            second = run_claude_consult(
                self_store, "/repo", "Explain finding two", session_registry=registry
            )
        self.assertEqual((first, second), ("First", "Follow-up"))
        first_command = popen.call_args_list[0].args[0]
        second_command = popen.call_args_list[1].args[0]
        self.assertNotIn("--resume", first_command)
        first_session_id = first_command[first_command.index("--session-id") + 1]
        self.assertEqual(
            second_command[second_command.index("--resume") + 1], first_session_id
        )
        second_input = second_process.communicate.call_args.kwargs["input"]
        self.assertEqual(second_input, "Explain finding two")

    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_explicit_fresh_context_replaces_session_after_success(
        self, popen, executable
    ):
        registry = ClaudeSessionRegistry()
        popen.side_effect = [
            self.process(result="First"),
            self.process(result="Fresh"),
            self.process(result="Continued fresh"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            self_store = self.store(directory)
            run_claude_consult(
                self_store, "/repo", "Review", session_registry=registry
            )
            run_claude_consult(
                self_store,
                "/repo",
                "Start again",
                new_context=True,
                session_registry=registry,
            )
            run_claude_consult(
                self_store, "/repo", "Follow up", session_registry=registry
            )
        reset_command = popen.call_args_list[1].args[0]
        follow_up_command = popen.call_args_list[2].args[0]
        self.assertNotIn("--resume", reset_command)
        reset_session_id = reset_command[reset_command.index("--session-id") + 1]
        self.assertEqual(
            follow_up_command[follow_up_command.index("--resume") + 1],
            reset_session_id,
        )

    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_failed_fresh_context_preserves_previous_session(self, popen, executable):
        registry = ClaudeSessionRegistry()
        popen.side_effect = [
            self.process(result="First"),
            self.process(result="Failed", stderr="provider failed", returncode=1),
            self.process(result="Continued"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            self_store = self.store(directory)
            run_claude_consult(
                self_store, "/repo", "Review", session_registry=registry
            )
            with self.assertRaisesRegex(AdapterError, "provider failed"):
                run_claude_consult(
                    self_store,
                    "/repo",
                    "Start again",
                    new_context=True,
                    session_registry=registry,
                )
            run_claude_consult(
                self_store, "/repo", "Continue old", session_registry=registry
            )
        final_command = popen.call_args_list[2].args[0]
        first_command = popen.call_args_list[0].args[0]
        first_session_id = first_command[first_command.index("--session-id") + 1]
        self.assertEqual(
            final_command[final_command.index("--resume") + 1], first_session_id
        )

    @patch("super_agent.mcp_server._terminate_process_group")
    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_cancelled_fresh_context_preserves_previous_session(
        self, popen, executable, terminate
    ):
        registry = ClaudeSessionRegistry()
        registry.replace("/repo", "main", SESSION_A)
        cancel_event = threading.Event()
        cancel_event.set()
        process = self.process(result="unused")
        process.poll.return_value = None
        popen.return_value = process
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AdapterError, "cancelled"):
                run_claude_consult(
                    self.store(directory),
                    "/repo",
                    "Start again",
                    new_context=True,
                    cancel_event=cancel_event,
                    session_registry=registry,
                )
        self.assertEqual(registry.get("/repo", "main"), SESSION_A)
        terminate.assert_called_once_with(process)

    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_sessions_are_isolated_by_claude_profile(self, popen, executable):
        registry = ClaudeSessionRegistry()
        popen.side_effect = [
            self.process(result="Main"),
            self.process(result="Second"),
            self.process(result="Main again"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            self_store = self.store(directory)
            self_store.load.side_effect = [
                {"agentDefaults": {"claude": "main"}},
                {"agentDefaults": {"claude": "second"}},
                {"agentDefaults": {"claude": "main"}},
            ]
            for prompt in ("Main", "Second", "Main again"):
                run_claude_consult(
                    self_store, "/repo", prompt, session_registry=registry
                )
        self.assertNotIn("--resume", popen.call_args_list[0].args[0])
        self.assertNotIn("--resume", popen.call_args_list[1].args[0])
        first_command = popen.call_args_list[0].args[0]
        main_session_id = first_command[first_command.index("--session-id") + 1]
        final_command = popen.call_args_list[2].args[0]
        self.assertEqual(
            final_command[final_command.index("--resume") + 1], main_session_id
        )

    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_plain_text_output_is_returned_and_session_is_saved(
        self, popen, executable
    ):
        registry = ClaudeSessionRegistry()
        popen.return_value = self.process(raw_stdout="SAVED\n")
        with tempfile.TemporaryDirectory() as directory:
            output = run_claude_consult(
                self.store(directory),
                "/repo",
                "Remember a codeword",
                session_registry=registry,
            )
        command = popen.call_args.args[0]
        assigned_session_id = command[command.index("--session-id") + 1]
        self.assertEqual(output, "SAVED")
        self.assertEqual(registry.get("/repo", "main"), assigned_session_id)

    @patch("super_agent.mcp_server._terminate_process_group")
    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_cancellation_terminates_the_claude_process_group(
        self, popen, executable, terminate
    ):
        cancel_event = threading.Event()
        cancel_event.set()
        process = self.process()
        process.poll.return_value = None
        popen.return_value = process
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AdapterError, "cancelled"):
                run_claude_consult(
                    self.store(directory),
                    "/repo",
                    "Inspect",
                    cancel_event=cancel_event,
                )
        terminate.assert_called_once_with(process)

    @patch("super_agent.mcp_server._terminate_process_group")
    @patch("super_agent.mcp_server.executable", return_value="/bin/claude")
    @patch("super_agent.mcp_server.subprocess.Popen")
    def test_timeout_terminates_the_claude_process_group(
        self, popen, executable, terminate
    ):
        process = self.process()
        process.poll.return_value = None
        popen.return_value = process
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AdapterError, "timed out"):
                run_claude_consult(
                    self.store(directory), "/repo", "Inspect", timeout=0
                )
        terminate.assert_called_once_with(process)

    def test_workspace_profile_lock_rejects_duplicate_consultation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            with consultation_lock(store, "/repo", "main"):
                with self.assertRaisesRegex(AdapterError, "already active"):
                    with consultation_lock(store, "/repo", "main"):
                        self.fail("duplicate consultation lock unexpectedly acquired")

    @patch("super_agent.mcp_server.executable", return_value="/usr/bin/git")
    @patch("super_agent.mcp_server.subprocess.run")
    def test_git_change_context_is_read_only_and_bounded(self, run, executable):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=" M src/app.py\n", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout="x" * (MAX_DIFF_CHARS + 500), stderr=""
            ),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        context = _current_change_context("/repo", {"PATH": "/usr/bin"})
        self.assertEqual(len(context), MAX_DIFF_CHARS)
        self.assertIn("change context truncated", context)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][0:3], ["/usr/bin/git", "status", "--short"])
        self.assertIn("--no-ext-diff", commands[1])
        self.assertIn("--no-textconv", commands[1])
        for call in run.call_args_list:
            self.assertFalse(call.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
