import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from super_agent.cli import _picker_lines, choose_profile, main
from super_agent.config import Store


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.codex_home = Path(self.temporary.name) / "codex"
        (self.codex_home / "sessions").mkdir(parents=True)
        (self.codex_home / "archived_sessions").mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "SUPER_AGENT_HOME": str(self.state),
                "CODEX_HOME": str(self.codex_home),
                "SUPER_CODEX_SHARED_CODEX_HOME": str(self.codex_home),
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def output(self, arguments):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(arguments)
        return code, stream.getvalue()

    def add_account_2(self):
        with patch("super_agent.cli.run_command", return_value=0):
            code, _ = self.output(
                ["profile", "add", "codex", "2", "--label", "Personal"]
            )
        self.assertEqual(code, 0)

    @patch("super_agent.cli.run_command", return_value=0)
    def test_profile_add_creates_profile_and_starts_login(self, run):
        code, output = self.output(["profile", "add", "codex", "2"])
        self.assertEqual(code, 0)
        self.assertIn("Added codex/2 (isolated)", output)
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["profiles"]["codex"]["2"]["label"], "Codex 2")
        command, env, cwd = run.call_args.args
        self.assertEqual(command, ["codex", "login"])
        self.assertIn("profiles/codex/2", env["CODEX_HOME"])
        self.assertEqual(cwd, str(Path.cwd().resolve()))

    def test_profile_add_overrides_existing_profile_only_after_successful_login(self):
        self.add_account_2()
        original_home = self.state / "profiles" / "codex" / "2"
        (original_home / "existing-state").write_text("original", encoding="utf-8")
        attempted_homes = []

        def cancel_login(command, env, cwd):
            attempted_homes.append(Path(env["CODEX_HOME"]))
            (attempted_homes[-1] / "partial-state").write_text(
                "partial", encoding="utf-8"
            )
            return 130

        with patch("super_agent.cli.run_command", side_effect=cancel_login):
            code, output = self.output(
                ["profile", "add", "codex", "2", "--label", "Replacement"]
            )
        self.assertEqual(code, 130)
        self.assertIn("will be overridden only after authentication succeeds", output)
        self.assertIn("Kept existing profile codex/2 unchanged", output)
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["profiles"]["codex"]["2"]["label"], "Personal")
        self.assertNotIn("providerHome", config["profiles"]["codex"]["2"])
        self.assertNotEqual(attempted_homes[-1], original_home)
        self.assertFalse(attempted_homes[-1].exists())
        self.assertEqual(
            (original_home / "existing-state").read_text(encoding="utf-8"),
            "original",
        )

        def complete_login(command, env, cwd):
            attempted_homes.append(Path(env["CODEX_HOME"]))
            (attempted_homes[-1] / "authenticated-state").write_text(
                "replacement", encoding="utf-8"
            )
            return 0

        with patch(
            "super_agent.cli.run_command", side_effect=complete_login
        ), patch("super_agent.cli.auth_status") as status:
            status.return_value.authenticated = True
            status.return_value.detail = "logged in"
            code, output = self.output(
                ["profile", "add", "codex", "2", "--label", "Replacement"]
            )
        self.assertEqual(code, 0)
        self.assertIn("Overrode codex/2 after successful authentication", output)
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["profiles"]["codex"]["2"]["label"], "Replacement")
        self.assertEqual(
            config["profiles"]["codex"]["2"]["providerHome"],
            attempted_homes[-1].name,
        )
        self.assertTrue((attempted_homes[-1] / "authenticated-state").exists())
        self.assertTrue(original_home.exists())
        self.assertEqual(
            Store(self.state).profile_home("codex", "2", config),
            attempted_homes[-1],
        )

    def test_profile_add_does_not_replace_when_login_exits_without_authentication(self):
        self.add_account_2()
        original_home = self.state / "profiles" / "codex" / "2"
        attempted_homes = []

        def incomplete_login(command, env, cwd):
            attempted_homes.append(Path(env["CODEX_HOME"]))
            return 0

        with patch(
            "super_agent.cli.run_command", side_effect=incomplete_login
        ), patch("super_agent.cli.auth_status") as status:
            status.return_value.authenticated = False
            status.return_value.detail = "not logged in"
            code, output = self.output(["profile", "add", "codex", "2"])
        self.assertEqual(code, 1)
        self.assertIn("without a verified authentication", output)
        self.assertIn("Kept existing profile codex/2 unchanged", output)
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("providerHome", config["profiles"]["codex"]["2"])
        self.assertEqual(
            Store(self.state).profile_home("codex", "2", config), original_home
        )
        self.assertFalse(attempted_homes[-1].exists())

    def test_default_dry_run_is_codex_main(self):
        code, output = self.output(["start", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("codex -C", output)
        self.assertIn("model-with-reasoning", output)
        self.assertIn("weekly-limit", output)
        self.assertNotIn("five-hour-limit", output)
        self.assertIn("git-branch", output)
        self.assertIn("current-dir", output)
        self.assertNotIn("CODEX_HOME=", output)

    def test_numbered_codex_profile_has_isolated_home(self):
        self.add_account_2()
        code, output = self.output(
            ["start", "--agent", "codex", "--profile", "2", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIn("CODEX_HOME=", output)
        self.assertIn("profiles/codex/2", output)

    def test_workspace_binding_changes_default_launch(self):
        self.add_account_2()
        code, _ = self.output(["use", "codex", "2"])
        self.assertEqual(code, 0)
        code, output = self.output(["start", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("CODEX_HOME=", output)

    def test_explicit_claude_override_uses_claude_default(self):
        code, output = self.output(["start", "--agent", "claude", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("claude", output)

    def test_codex_launch_injects_read_only_claude_mcp_server(self):
        code, output = self.output(["start", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("mcp_servers.super_codex_claude.command", output)
        self.assertIn("mcp-server", output)
        self.assertIn("enabled_tools", output)
        self.assertIn("ask_claude", output)

    def test_reasoning_override_is_forwarded_to_codex(self):
        code, output = self.output(["start", "--reasoning", "low", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("model_reasoning_effort", output)
        self.assertIn("low", output)

    @patch("super_agent.cli.profile_rows", return_value=[])
    def test_status_identifies_active_binding(self, profile_rows):
        self.add_account_2()
        self.output(["use", "codex", "2"])
        code, output = self.output(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Active:    codex/2", output)

    @patch("super_agent.cli.profile_rows", return_value=[])
    def test_usage_all_targets_both_codex_profiles(self, profile_rows):
        self.add_account_2()
        code, _ = self.output(["usage", "--all"])
        self.assertEqual(code, 0)
        targets = profile_rows.call_args.kwargs["only"]
        self.assertEqual(targets, {("codex", "main"), ("codex", "2")})

    @patch("super_agent.cli.profile_rows", return_value=[])
    def test_usage_explains_codex_target_when_claude_is_active(self, profile_rows):
        self.output(["use", "claude", "main"])
        code, output = self.output(["usage"])
        self.assertEqual(code, 0)
        self.assertIn("Claude is active; showing Codex usage for codex/main", output)

    @patch("super_agent.cli.profile_rows")
    def test_profiles_json_is_machine_readable(self, profile_rows):
        profile_rows.return_value = [
            {
                "agent": "codex",
                "profile": "main",
                "label": "Codex 1",
                "isolation": "shared",
                "authenticated": True,
                "authDetail": "ready",
                "live": [],
            }
        ]
        code, output = self.output(["profiles", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)[0]["profile"], "main")

    @patch("super_agent.cli.profile_rows", return_value=[])
    def test_status_json_includes_selection(self, profile_rows):
        code, output = self.output(["status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["active"], {"agent": "codex", "profile": "main"})

    def test_bindings_support_text_and_json(self):
        self.add_account_2()
        self.output(["use", "codex", "2"])
        code, output = self.output(["bindings"])
        self.assertEqual(code, 0)
        self.assertIn("-> codex/2", output)
        code, output = self.output(["bindings", "--json"])
        self.assertEqual(json.loads(output)["bindings"][0]["profile"], "2")

    def test_profile_label_command(self):
        self.add_account_2()
        code, output = self.output(["profile", "label", "codex", "2", "Work"])
        self.assertEqual(code, 0)
        self.assertIn("Labeled codex/2 as Work", output)
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["profiles"]["codex"]["2"]["label"], "Work")

    def test_profile_main_command_designates_the_default_account(self):
        self.add_account_2()
        code, output = self.output(["profile", "main", "codex", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(output, "Main Codex account: codex/2\n")
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["defaults"], {"agent": "codex", "profile": "2"})
        self.assertEqual(config["agentDefaults"]["codex"], "2")
        code, output = self.output(["profiles", "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(output)
        self.assertFalse(next(row for row in rows if row["profile"] == "main")["main"])
        self.assertTrue(next(row for row in rows if row["profile"] == "2")["main"])

    def test_profile_main_rejects_an_unknown_account(self):
        code, _ = self.output(["profile", "main", "codex", "2"])
        self.assertEqual(code, 2)

    @patch("super_agent.cli.executable", return_value="/bin/agent")
    @patch("super_agent.cli.profile_rows")
    def test_setup_gives_numbered_account_next_step(self, profile_rows, executable):
        profile_rows.return_value = [
            {"agent": "codex", "profile": "main", "label": "Codex 1", "isolation": "shared", "authenticated": True, "authDetail": "ready", "live": []},
            {"agent": "codex", "profile": "2", "label": "Codex 2", "isolation": "isolated", "authenticated": False, "authDetail": "not logged in", "live": []},
            {"agent": "claude", "profile": "main", "label": "Claude", "isolation": "shared", "authenticated": True, "authDetail": "ready", "live": []},
        ]
        code, output = self.output(["setup"])
        self.assertEqual(code, 0)
        self.assertIn("sc login --profile 2", output)

    @patch("super_agent.cli.executable", return_value="/bin/agent")
    @patch("super_agent.cli.profile_rows")
    def test_setup_uses_configured_claude_default_profile(
        self, profile_rows, executable
    ):
        config = Store(self.state).load()
        Store(self.state).add_profile(config, "claude", "reviewer", "Reviewer")
        config["agentDefaults"]["claude"] = "reviewer"
        Store(self.state).save(config)
        profile_rows.return_value = [
            {"agent": "codex", "profile": "main", "label": "Codex 1", "isolation": "shared", "authenticated": True, "authDetail": "ready", "live": []},
            {"agent": "claude", "profile": "main", "label": "Claude", "isolation": "shared", "authenticated": True, "authDetail": "ready", "live": []},
            {"agent": "claude", "profile": "reviewer", "label": "Reviewer", "isolation": "isolated", "authenticated": False, "authDetail": "not logged in", "live": []},
        ]
        code, output = self.output(["setup"])
        self.assertEqual(code, 0)
        self.assertIn("--profile reviewer", output)

    def test_number_is_a_direct_start_shorthand(self):
        self.add_account_2()
        code, output = self.output(["2", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("profiles/codex/2", output)

    def test_main_is_a_direct_start_shorthand(self):
        code, output = self.output(["main", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("codex -C", output)
        self.assertNotIn("profiles/codex/", output)

    def test_main_shorthand_launches_the_designated_account(self):
        self.add_account_2()
        self.output(["profile", "main", "codex", "2"])
        code, output = self.output(["main", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("profiles/codex/2", output)

    def test_one_shorthand_still_launches_the_shared_account(self):
        self.add_account_2()
        self.output(["profile", "main", "codex", "2"])
        code, output = self.output(["1", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertNotIn("profiles/codex/", output)

    def test_main_shorthand_honors_an_explicit_agent(self):
        self.add_account_2()
        self.output(["profile", "main", "codex", "2"])
        code, output = self.output(["main", "--agent", "claude", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("claude", output)
        self.assertNotIn("CODEX_HOME=", output)

    def test_profile_order_command_persists_picker_order(self):
        self.add_account_2()
        with patch("super_agent.cli.run_command", return_value=0):
            self.output(["profile", "add", "codex", "3"])
        code, output = self.output(["profile", "order", "codex", "3", "main", "2"])
        self.assertEqual(code, 0)
        self.assertIn("3 main 2", output)
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["profileOrder"]["codex"], ["3", "main", "2"])

    def test_config_mode_changes_bare_sc_behavior(self):
        self.add_account_2()
        self.output(["use", "codex", "2"])
        code, output = self.output(["config", "mode", "main"])
        self.assertEqual((code, output), (0, "main\n"))
        with patch("super_agent.cli.exec_command", return_value=0) as execute:
            code, _ = self.output([])
        self.assertEqual(code, 0)
        self.assertEqual(execute.call_args.args[-1], "codex")
        self.assertIn("profiles/codex/2", execute.call_args.args[1]["CODEX_HOME"])

    @patch("super_agent.cli.choose_profile")
    @patch("super_agent.cli.exec_command", return_value=0)
    def test_bare_sc_launches_bound_claude_without_codex_picker(
        self, execute, choose
    ):
        self.output(["use", "claude", "main"])
        code, _ = self.output([])
        self.assertEqual(code, 0)
        choose.assert_not_called()
        self.assertEqual(execute.call_args.args[-1], "claude")

    @patch("super_agent.cli.exec_command", return_value=0)
    @patch("super_agent.cli.choose_profile", return_value="2")
    @patch("super_agent.cli.profile_rows")
    def test_bare_sc_picker_uses_live_rows_and_selected_profile(self, rows, choose, execute):
        self.add_account_2()
        rows.return_value = [
            {"agent": "codex", "profile": "main"},
            {"agent": "codex", "profile": "2"},
        ]
        code, _ = self.output([])
        self.assertEqual(code, 0)
        self.assertTrue(rows.call_args.kwargs["live"])
        self.assertEqual(choose.call_args.args[0], rows.return_value)
        self.assertEqual(choose.call_args.kwargs["initial_profile"], "main")
        self.assertIn("profiles/codex/2", execute.call_args.args[1]["CODEX_HOME"])

    @patch("super_agent.cli.exec_command", return_value=0)
    @patch("super_agent.cli.choose_profile", return_value="2")
    @patch("super_agent.cli.profile_rows")
    def test_bare_sc_picker_preselects_workspace_binding(self, rows, choose, execute):
        self.add_account_2()
        self.output(["use", "codex", "2"])
        rows.return_value = [
            {"agent": "codex", "profile": "main"},
            {"agent": "codex", "profile": "2"},
        ]
        code, _ = self.output([])
        self.assertEqual(code, 0)
        self.assertEqual(choose.call_args.kwargs["initial_profile"], "2")

    def test_picker_displays_identity_and_current_limits(self):
        lines = _picker_lines(
            [{
                "profile": "2",
                "label": "Personal",
                "main": True,
                "authenticated": True,
                "authDetail": "logged in",
                "live": ["account: me@example.com (plus)", "5h: 25% used"],
            }],
            0,
        )
        rendered = "\n".join(lines)
        self.assertIn("Personal (main)", rendered)
        self.assertIn("me@example.com", rendered)
        self.assertIn("5h: 25% used", rendered)

    def test_picker_arrow_key_changes_selection(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 99

        rows = [
            {"profile": "main", "label": "Work", "authenticated": True, "authDetail": "", "live": []},
            {"profile": "2", "label": "Personal", "authenticated": True, "authDetail": "", "live": []},
        ]
        output = FakeTTY()
        with patch("super_agent.cli.termios.tcgetattr", return_value=[]), patch(
            "super_agent.cli.termios.tcsetattr"
        ), patch("super_agent.cli.tty.setcbreak"), patch(
            "super_agent.cli.select.select", return_value=([99], [], [])
        ), patch("super_agent.cli.os.read", side_effect=[b"\x1b", b"[", b"B", b"\r"]):
            selected = choose_profile(rows, FakeTTY(), output)
        self.assertEqual(selected, "2")
        self.assertIn("\r\n", output.getvalue())

    def test_picker_ignores_other_complete_escape_sequences(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 99

        rows = [
            {"profile": "main", "label": "Work", "authenticated": True, "authDetail": "", "live": []},
            {"profile": "2", "label": "Personal", "authenticated": True, "authDetail": "", "live": []},
        ]
        with patch("super_agent.cli.termios.tcgetattr", return_value=[]), patch(
            "super_agent.cli.termios.tcsetattr"
        ), patch("super_agent.cli.tty.setcbreak"), patch(
            "super_agent.cli.select.select", return_value=([99], [], [])
        ), patch(
            "super_agent.cli.os.read",
            side_effect=[b"\x1b", b"[", b"6", b"~", b"\r"],
        ):
            selected = choose_profile(rows, FakeTTY(), FakeTTY(), initial_profile="2")
        self.assertEqual(selected, "2")

    def test_picker_eof_cancels(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 99

        rows = [
            {"profile": "main", "label": "Work", "authenticated": True, "authDetail": "", "live": []}
        ]
        with patch("super_agent.cli.termios.tcgetattr", return_value=[]), patch(
            "super_agent.cli.termios.tcsetattr"
        ), patch("super_agent.cli.tty.setcbreak"), patch(
            "super_agent.cli.os.read", return_value=b""
        ):
            self.assertIsNone(choose_profile(rows, FakeTTY(), FakeTTY()))

    def test_unusable_profile_does_not_abort_listing_or_doctor(self):
        self.add_account_2()
        sessions = self.state / "profiles" / "codex" / "2" / "sessions"
        sessions.unlink()
        sessions.mkdir()
        (sessions / "existing.jsonl").write_text("local", encoding="utf-8")

        code, output = self.output(["profiles"])
        self.assertEqual(code, 0)
        self.assertIn("codex/2", output)
        self.assertIn("profile unavailable", output)

        code, output = self.output(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL codex/2", output)

    def test_picker_ctrl_c_cancels_without_traceback(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 99

        output = FakeTTY()
        rows = [
            {
                "profile": "main",
                "label": "Work",
                "authenticated": True,
                "authDetail": "",
                "live": [],
            }
        ]
        with patch("super_agent.cli.termios.tcgetattr", return_value=[]), patch(
            "super_agent.cli.termios.tcsetattr"
        ), patch("super_agent.cli.tty.setcbreak"), patch(
            "super_agent.cli.os.read", side_effect=KeyboardInterrupt
        ):
            selected = choose_profile(rows, FakeTTY(), output)
        self.assertIsNone(selected)
        self.assertIn("\x1b[?25h\x1b[?1049l", output.getvalue())

    @patch("super_agent.cli.choose_profile", side_effect=KeyboardInterrupt)
    @patch("super_agent.cli.profile_rows", return_value=[])
    def test_bare_sc_handles_picker_interrupt_without_traceback(self, rows, choose):
        code, output = self.output([])
        self.assertEqual(code, 130)
        self.assertEqual(output, "Account selection cancelled.\n")

    def test_legacy_second_profile_name_is_rejected(self):
        code, _ = self.output(["profile", "add", "codex", "second"])
        self.assertEqual(code, 2)

    def test_config_contains_no_credentials(self):
        self.output(["start", "--dry-run"])
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        serialized = json.dumps(config).lower()
        self.assertNotIn("token", serialized)
        self.assertNotIn("credential", serialized)
        self.assertNotIn("api_key", serialized)


if __name__ == "__main__":
    unittest.main()
