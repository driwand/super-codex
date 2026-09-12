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
    codex_limits_exhausted,
    codex_live_status,
    codex_set_thread_names,
    codex_thread_names,
    exec_command,
    format_codex_live,
    run_command,
    summarize_failure,
)


class FailureSummaryTests(unittest.TestCase):
    def test_an_expired_token_reads_as_a_refresh_not_a_failure(self):
        message = (
            "failed to fetch codex rate limits: GET https://example.test/usage "
            'failed: 401 Unauthorized; content-type=text/plain; body={\n'
            '  "error": {\n    "code": "token_expired"\n  }\n}'
        )
        summary = summarize_failure(AdapterError(message))
        self.assertEqual(
            summary, "sign-in token expired; it refreshes when this account next runs"
        )

    def test_an_unrecognised_failure_stays_one_short_line(self):
        summary = summarize_failure(AdapterError("broke\nin\ntwo places " + "x" * 400))
        self.assertNotIn("\n", summary)
        self.assertLessEqual(len(summary), 140)
        self.assertTrue(summary.startswith("broke in two places"))

    def test_a_short_failure_is_passed_through(self):
        self.assertEqual(summarize_failure(AdapterError("codex is not installed")),
                         "codex is not installed")


class CommandTests(unittest.TestCase):
    def test_codex_is_default_interactive_shape(self):
        command = build_command("codex", "start", "/repo", prompt="fix it", model="gpt-test")
        self.assertEqual(
            command,
            [
                "codex",
                "-C",
                "/repo",
                "-c",
                'tui.status_line=["model-with-reasoning", "five-hour-limit", "weekly-limit", "git-branch", "current-dir"]',
                "--model",
                "gpt-test",
                "fix it",
            ],
        )

    def test_numbered_codex_account_does_not_change_command(self):
        command = build_command("codex", "ask", "/repo", prompt="inspect")
        self.assertEqual(
            command,
            ["codex", "exec", "-C", "/repo", "--skip-git-repo-check", "inspect"],
        )

    def test_codex_resume_last(self):
        command = build_command("codex", "resume", "/repo", use_last=True)
        self.assertEqual(
            command,
            [
                "codex",
                "resume",
                "-C",
                "/repo",
                "--last",
                "-c",
                'tui.status_line=["model-with-reasoning", "five-hour-limit", "weekly-limit", "git-branch", "current-dir"]',
            ],
        )

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
            'mcp_servers.super_codex_claude.enabled_tools='
            '["ask_claude","claude_job_status","cancel_claude_job"]',
            command,
        )
        self.assertIn(
            'mcp_servers.super_codex_claude.tools.ask_claude.approval_mode="auto"',
            command,
        )
        self.assertIn(
            'mcp_servers.super_codex_claude.tools.'
            'claude_job_status.approval_mode="auto"',
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
    @patch("super_agent.adapters.run_command", return_value=7)
    def test_exec_command_waits_and_runs_the_post_exit_callback(self, run, executable):
        callbacks = []

        status = exec_command(
            ["codex"], {}, "/repo", agent="codex", after=lambda: callbacks.append(True)
        )

        self.assertEqual(status, 7)
        run.assert_called_once_with(["codex"], {}, "/repo")
        self.assertEqual(callbacks, [True])

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters.subprocess.run", side_effect=KeyboardInterrupt)
    def test_run_command_normalizes_interrupted_login(self, run, executable):
        self.assertEqual(run_command(["codex", "login"], {}, "/repo"), 130)


class ThreadNameTests(unittest.TestCase):
    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters._app_server_requests")
    def test_thread_names_follow_paginated_app_server_results(self, exchange, executable):
        exchange.side_effect = [
            (
                {
                    2: {
                        "id": 2,
                        "result": {
                            "data": [
                                {"id": "thread-1", "name": "First", "updatedAt": 10},
                                {"id": "thread-2", "name": None, "updatedAt": 20},
                            ],
                            "nextCursor": "next-page",
                        },
                    }
                },
                [],
                0,
            ),
            (
                {
                    2: {
                        "id": 2,
                        "result": {
                            "data": [
                                {"id": "thread-3", "name": "Third", "updatedAt": 30}
                            ],
                            "nextCursor": None,
                        },
                    }
                },
                [],
                0,
            ),
        ]

        names = codex_thread_names({"CODEX_HOME": "/codex"})

        self.assertEqual(names["thread-1"], {"name": "First", "updatedAt": 10})
        self.assertEqual(names["thread-2"], {"name": None, "updatedAt": 20})
        self.assertEqual(names["thread-3"], {"name": "Third", "updatedAt": 30})
        first = exchange.call_args_list[0].args[1][-1]
        second = exchange.call_args_list[1].args[1][-1]
        self.assertFalse(first["params"]["useStateDbOnly"])
        self.assertNotIn("cursor", first["params"])
        self.assertEqual(second["params"]["cursor"], "next-page")

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters._app_server_requests")
    def test_thread_names_surface_protocol_errors(self, exchange, executable):
        exchange.return_value = (
            {2: {"id": 2, "error": {"message": "unsupported"}}},
            [],
            0,
        )
        with self.assertRaisesRegex(AdapterError, "unsupported"):
            codex_thread_names({"CODEX_HOME": "/codex"})

    @patch("super_agent.adapters.executable", return_value="/bin/codex")
    @patch("super_agent.adapters._app_server_requests")
    def test_sets_names_through_the_app_server(self, exchange, executable):
        exchange.return_value = (
            {2: {"id": 2, "result": {}}, 3: {"id": 3, "result": {}}},
            [],
            0,
        )

        applied = codex_set_thread_names(
            {"CODEX_HOME": "/codex"}, {"thread-2": "Second", "thread-1": "First"}
        )

        self.assertEqual(applied, 2)
        requests = exchange.call_args.args[1]
        self.assertEqual(requests[2]["method"], "thread/name/set")
        self.assertEqual(
            [request["params"] for request in requests[2:]],
            [
                {"threadId": "thread-1", "name": "First"},
                {"threadId": "thread-2", "name": "Second"},
            ],
        )
        self.assertTrue(requests[0]["params"]["capabilities"]["experimentalApi"])


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


class CodexExhaustionTests(unittest.TestCase):
    def status(self, snapshot):
        return LiveStatus(
            account={"email": "person@example.com"},
            rate_limits={"rateLimitsByLimitId": {"codex": snapshot}},
        )

    def test_a_spent_window_is_exhausted(self):
        snapshot = {
            "primary": {"usedPercent": 100, "windowDurationMins": 300},
            "secondary": {"usedPercent": 16, "windowDurationMins": 10080},
        }
        self.assertTrue(codex_limits_exhausted(self.status(snapshot)))

    def test_a_nearly_spent_window_is_still_usable(self):
        snapshot = {
            "primary": {"usedPercent": 98, "windowDurationMins": 300},
            "secondary": {"usedPercent": 15, "windowDurationMins": 10080},
        }
        self.assertFalse(codex_limits_exhausted(self.status(snapshot)))

    def test_either_window_can_exhaust_the_account(self):
        snapshot = {
            "primary": {"usedPercent": 4, "windowDurationMins": 300},
            "secondary": {"usedPercent": 100, "windowDurationMins": 10080},
        }
        self.assertTrue(codex_limits_exhausted(self.status(snapshot)))

    def test_reported_limit_and_spend_flags_are_believed(self):
        self.assertTrue(codex_limits_exhausted(self.status({"rateLimitReachedType": "primary"})))
        self.assertTrue(codex_limits_exhausted(self.status({"spendControlReached": True})))

    def test_spent_individual_limit_is_exhausted(self):
        snapshot = {"individualLimit": {"remainingPercent": 0, "used": "6500", "limit": "6500"}}
        self.assertTrue(codex_limits_exhausted(self.status(snapshot)))

    def test_unlimited_credits_outlast_a_spent_window(self):
        snapshot = {
            "primary": {"usedPercent": 100, "windowDurationMins": 300},
            "credits": {"unlimited": True},
        }
        self.assertFalse(codex_limits_exhausted(self.status(snapshot)))

    def test_unreported_usage_is_treated_as_available(self):
        self.assertFalse(codex_limits_exhausted(self.status({"primary": {}})))
        self.assertFalse(
            codex_limits_exhausted(LiveStatus(account={}, rate_limits={}))
        )

    def test_flat_rate_limit_payload_is_read(self):
        status = LiveStatus(
            account={},
            rate_limits={"rateLimits": {"primary": {"usedPercent": 100}}},
        )
        self.assertTrue(codex_limits_exhausted(status))


if __name__ == "__main__":
    unittest.main()


class ProviderCommandTests(unittest.TestCase):
    def test_codex_start_passes_the_profile_flag(self):
        command = build_command("codex", "start", "/tmp", provider="explabs")
        self.assertIn("--profile", command)
        self.assertEqual(command[command.index("--profile") + 1], "explabs")

    def test_provider_flag_precedes_native_arguments(self):
        command = build_command(
            "codex", "start", "/tmp", provider="explabs", native=["--search"]
        )
        self.assertLess(command.index("--profile"), command.index("--search"))

    def test_no_provider_leaves_the_command_untouched(self):
        self.assertNotIn("--profile", build_command("codex", "start", "/tmp"))

    def test_exec_keeps_the_provider_flag(self):
        command = build_command("codex", "ask", "/tmp", prompt="hi", provider="explabs")
        self.assertIn("--profile", command)
        self.assertEqual(command[-1], "hi")
