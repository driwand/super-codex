import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from super_agent.adapters import (
    CLAUDE_ROUTING_INSTRUCTIONS,
    AdapterError,
    LiveStatus,
    auth_status,
    build_command,
    codex_live_status,
    exec_command,
    format_codex_live,
    run_command,
)


class CommandTests(unittest.TestCase):
    def test_codex_is_default_interactive_shape(self):
        command = build_command("codex", "start", "/repo", prompt="fix it", model="gpt-test")
        self.assertEqual(
            command,
            ["codex", "-C", "/repo", "--model", "gpt-test", "fix it"],
        )

    def test_numbered_codex_account_does_not_change_command(self):
        command = build_command("codex", "ask", "/repo", prompt="inspect")
        self.assertEqual(
            command,
            ["codex", "exec", "-C", "/repo", "--skip-git-repo-check", "inspect"],
        )

    def test_codex_resume_last(self):
        command = build_command("codex", "resume", "/repo", use_last=True)
        self.assertEqual(command, ["codex", "resume", "-C", "/repo", "--last"])

    def test_codex_launch_can_inject_claude_mcp_without_shell(self):
        command = build_command(
            "codex",
            "start",
            "/repo",
            mcp_command="/opt/Super Codex/bin/super-codex",
        )
        self.assertEqual(command[0:3], ["codex", "-C", "/repo"])
        self.assertIn(
            'mcp_servers.super_codex_claude.command="/opt/Super Codex/bin/super-codex"',
            command,
        )
        self.assertIn('mcp_servers.super_codex_claude.args=["mcp-server"]', command)
        self.assertIn("mcp_servers.super_codex_claude.required=true", command)
        self.assertIn(
            'mcp_servers.super_codex_claude.enabled_tools=["ask_claude"]', command
        )
        self.assertIn(
            'mcp_servers.super_codex_claude.tools.ask_claude.approval_mode="auto"',
            command,
        )
        self.assertIn("mcp_servers.super_codex_claude.tool_timeout_sec=180", command)
        self.assertIn(
            f"developer_instructions={json.dumps(CLAUDE_ROUTING_INSTRUCTIONS)}", command
        )
        self.assertIn("include_diff=true", CLAUDE_ROUTING_INSTRUCTIONS)
        self.assertIn("new_context=true", CLAUDE_ROUTING_INSTRUCTIONS)
        self.assertIn("never retry automatically", CLAUDE_ROUTING_INSTRUCTIONS)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_codex_reasoning_override_is_explicit(self):
        command = build_command("codex", "start", "/repo", reasoning="low")
        self.assertIn('model_reasoning_effort="low"', command)

    def test_claude_resume_picker_and_last(self):
        self.assertEqual(build_command("claude", "resume", "/repo"), ["claude", "-r"])
        self.assertEqual(
            build_command("claude", "resume", "/repo", use_last=True),
            ["claude", "-c"],
        )

    def test_native_arguments_are_forwarded_without_permission_bypass(self):
        command = build_command(
            "claude", "start", "/repo", native=["--permission-mode", "plan"]
        )
        self.assertEqual(command, ["claude", "--permission-mode", "plan"])

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters.os.chdir", side_effect=OSError("missing workspace"))
    def test_exec_command_reports_working_directory_failure(self, chdir, executable):
        with self.assertRaisesRegex(AdapterError, "missing workspace"):
            exec_command(["codex"], {}, "/missing", agent="codex")

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters.subprocess.run", side_effect=KeyboardInterrupt)
    def test_run_command_normalizes_interrupted_login(self, run, executable):
        self.assertEqual(run_command(["codex", "login"], {}, "/repo"), 130)


class StatusTests(unittest.TestCase):
    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters._app_server_requests")
    def test_codex_live_status_uses_app_server_without_reading_credentials(self, exchange, executable):
        exchange.return_value = (
            {
                2: {
                    "id": 2,
                    "result": {
                        "account": {
                            "type": "chatgpt",
                            "email": "person@example.com",
                            "planType": "plus",
                        }
                    },
                },
                3: {
                    "id": 3,
                    "result": {
                        "rateLimits": {
                            "limitId": "codex",
                            "primary": {
                                "usedPercent": 25,
                                "windowDurationMins": 300,
                                "resetsAt": 1900000000,
                            },
                        }
                    },
                },
            },
            [],
            0,
        )
        with tempfile.TemporaryDirectory() as directory:
            sqlite_home = Path(directory) / "sqlite"
            status = codex_live_status({"PATH": "/bin"}, sqlite_home=sqlite_home)
            process_env, messages, _ = exchange.call_args.args
            self.assertEqual(process_env["CODEX_SQLITE_HOME"], str(sqlite_home.absolute()))
        self.assertEqual(status.account["email"], "person@example.com")
        self.assertEqual(status.rate_limits["rateLimits"]["primary"]["usedPercent"], 25)
        request = json.dumps(messages)
        self.assertIn("account/read", request)
        self.assertIn("account/rateLimits/read", request)
        self.assertNotIn("auth.json", request)

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters._app_server_requests")
    def test_codex_live_status_surfaces_protocol_error(self, exchange, executable):
        exchange.return_value = (
            {3: {"id": 3, "error": {"message": "authentication required"}}},
            [],
            0,
        )
        with self.assertRaisesRegex(AdapterError, "authentication required"):
            codex_live_status({})

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters._app_server_requests")
    def test_codex_live_status_surfaces_initialization_error(self, exchange, executable):
        exchange.return_value = (
            {1: {"id": 1, "error": {"message": "unsupported protocol"}}},
            ["unsupported protocol"],
            1,
        )
        with self.assertRaisesRegex(AdapterError, "unsupported protocol"):
            codex_live_status({})

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters._app_server_requests")
    def test_codex_live_status_surfaces_timeout(self, exchange, executable):
        exchange.return_value = ({1: {"id": 1, "result": {}}}, ["Codex usage request timed out"], -15)
        with self.assertRaisesRegex(AdapterError, "timed out"):
            codex_live_status({})

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    def test_codex_live_status_rejects_symlinked_runtime(self, executable):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            linked = root / "runtime"
            linked.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(AdapterError, "private directory"):
                codex_live_status({}, sqlite_home=linked)

    @patch("super_agent.adapters.executable", return_value="/bin/claude")
    @patch("super_agent.adapters.subprocess.run")
    def test_claude_auth_status_only_extracts_identity_fields(self, run, executable):
        run.return_value = subprocess.CompletedProcess(
            ["claude"],
            0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "email": "person@example.com",
                    "apiProvider": "firstParty",
                    "unrelatedSecret": "must-not-appear",
                }
            ),
            stderr="",
        )
        status = auth_status("claude", {})
        self.assertTrue(status.authenticated)
        self.assertEqual(status.detail, "person@example.com / firstParty")
        self.assertNotIn("must-not-appear", status.detail)

    def test_formats_verified_codex_windows(self):
        status = LiveStatus(
            account={"email": "person@example.com", "planType": "plus"},
            rate_limits={
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitName": "Codex",
                        "primary": {"usedPercent": 20, "windowDurationMins": 300},
                        "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
                    }
                }
            },
        )
        lines = format_codex_live(status)
        self.assertEqual(lines[0], "account: person@example.com (plus)")
        self.assertIn("5h: 20% used", lines[1])
        self.assertIn("7d: 40% used", lines[1])

    def test_formats_usage_based_spend_limit(self):
        status = LiveStatus(
            account={"type": "chatgpt", "planType": "business"},
            rate_limits={
                "rateLimits": {
                    "limitId": "codex",
                    "individualLimit": {
                        "remainingPercent": 53,
                        "used": "3066.329453",
                        "limit": "6500",
                        "resetsAt": 1900000000,
                    },
                }
            },
        )
        lines = format_codex_live(status)
        self.assertIn("53% remaining", lines[1])
        self.assertIn("3066.33/6500", lines[1])


if __name__ == "__main__":
    unittest.main()
