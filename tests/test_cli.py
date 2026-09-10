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

from super_agent.cli import (
    _initial_index,
    _picker_lines,
    choose_profile,
    main,
    reconcile_session_names,
)
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

    @patch("super_agent.cli.run_update", return_value=0)
    def test_upd_is_an_update_alias(self, run_update):
        code, _ = self.output(["upd"])
        self.assertEqual(code, 0)
        run_update.assert_called_once_with(False, None)

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
        self.assertIn("five-hour-limit", output)
        self.assertIn("weekly-limit", output)
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

    def test_every_codex_profile_uses_the_same_rate_limit_status_line(self):
        self.add_account_2()
        expected = (
            'tui.status_line=["model-with-reasoning", "five-hour-limit", '
            '"weekly-limit", "git-branch", "current-dir"]'
        )
        for profile in ("main", "2"):
            code, output = self.output(
                ["start", "--agent", "codex", "--profile", profile, "--dry-run"]
            )
            self.assertEqual(code, 0)
            self.assertIn(expected, output)

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
        self.assertEqual(
            payload["active"],
            {"agent": "codex", "profile": "main", "provider": "default"},
        )

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

    def test_picker_marks_a_drained_account(self):
        lines = _picker_lines(
            [{
                "profile": "main",
                "label": "Work",
                "main": True,
                "authenticated": True,
                "authDetail": "",
                "live": ["5h: 100% used"],
                "exhausted": True,
            }],
            0,
        )
        self.assertIn("[limits spent]", "\n".join(lines))

    def rows_for_cursor(self, main_exhausted=False, second_exhausted=False):
        return [
            {
                "profile": "main",
                "label": "Work",
                "main": True,
                "authenticated": True,
                "authDetail": "",
                "live": [],
                "exhausted": main_exhausted,
            },
            {
                "profile": "2",
                "label": "Personal",
                "authenticated": True,
                "authDetail": "",
                "live": [],
                "exhausted": second_exhausted,
            },
        ]

    def test_cursor_stays_on_main_while_it_has_limits_left(self):
        self.assertEqual(_initial_index(self.rows_for_cursor(), "main"), 0)

    def test_cursor_skips_a_drained_main(self):
        rows = self.rows_for_cursor(main_exhausted=True)
        self.assertEqual(_initial_index(rows, "main"), 1)

    def test_cursor_wraps_to_reach_accounts_listed_before_the_bound_one(self):
        rows = self.rows_for_cursor(second_exhausted=True)
        self.assertEqual(_initial_index(rows, "2"), 0)

    def test_cursor_keeps_main_when_every_account_is_drained(self):
        rows = self.rows_for_cursor(main_exhausted=True, second_exhausted=True)
        self.assertEqual(_initial_index(rows, "main"), 0)

    def test_cursor_stays_put_when_the_bound_account_status_is_unknown(self):
        # A lapsed login or a probe that failed is not a spent limit. Moving the
        # cursor on that reading would silently start a different identity in a
        # workspace bound to this one.
        rows = self.rows_for_cursor()
        rows[0]["authenticated"] = False
        self.assertEqual(_initial_index(rows, "main"), 0)
        rows = self.rows_for_cursor()
        rows[0]["error"] = "profile unavailable"
        self.assertEqual(_initial_index(rows, "main"), 0)

    def test_cursor_only_lands_on_an_account_that_can_run(self):
        rows = self.rows_for_cursor(main_exhausted=True)
        rows[1]["authenticated"] = False
        self.assertEqual(_initial_index(rows, "main"), 0)

    def test_drained_main_is_not_what_enter_selects(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 99

        rows = self.rows_for_cursor(main_exhausted=True)
        with patch("super_agent.cli.termios.tcgetattr", return_value=[]), patch(
            "super_agent.cli.termios.tcsetattr"
        ), patch("super_agent.cli.tty.setcbreak"), patch(
            "super_agent.cli.os.read", side_effect=[b"\r"]
        ):
            selected = choose_profile(rows, FakeTTY(), FakeTTY(), initial_profile="main")
        self.assertEqual(selected, "2")

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
        if sessions.is_symlink() or sessions.is_file():
            sessions.unlink()
        else:
            sessions.rmdir()
        sessions.write_text("not a directory", encoding="utf-8")

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


class ProviderCliTests(unittest.TestCase):
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
            code, _ = self.output(["profile", "add", "codex", "2", "--label", "Personal"])
        self.assertEqual(code, 0)

    def add_provider(self, name="explabs"):
        code, output = self.output(
            [
                "provider",
                "add",
                name,
                "--base-url",
                "https://api.example.com/v1",
                "--env-key",
                "EXAMPLE_API_KEY",
                "--model",
                "example-model",
                "--label",
                "Example",
            ]
        )
        self.assertEqual(code, 0)
        return output

    def test_add_lists_and_warns_about_a_missing_credential(self):
        output = self.add_provider()
        self.assertIn("Added provider explabs", output)
        self.assertIn("export EXAMPLE_API_KEY", output)
        code, listing = self.output(["provider", "list"])
        self.assertEqual(code, 0)
        self.assertIn("explabs", listing)
        self.assertIn("NOT SET", listing)

    def test_list_marks_the_default_as_active(self):
        code, listing = self.output(["provider", "list"])
        self.assertEqual(code, 0)
        self.assertIn("* default", listing)

    def test_launch_without_a_provider_has_no_profile_flag(self):
        self.add_provider()
        code, output = self.output(["start", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertNotIn("--profile explabs", output)

    def test_launch_with_provider_flag_adds_the_profile(self):
        self.add_provider()
        code, output = self.output(["start", "--provider", "explabs", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("--profile explabs", output)

    def test_bound_provider_applies_without_a_flag(self):
        self.add_provider()
        self.output(["provider", "use", "explabs"])
        code, output = self.output(["start", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("--profile explabs", output)
        code, output = self.output(["start", "--provider", "default", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("codex", output)
        self.assertNotIn("--profile explabs", output)

    def test_launch_materializes_the_profile_into_the_codex_home(self):
        self.add_provider()
        with patch("super_agent.cli.exec_command", return_value=0) as run:
            code = main(["start", "--provider", "explabs"])
        self.assertEqual(code, 0)
        self.assertIn("--profile", run.call_args.args[0])
        written = self.codex_home / "explabs.config.toml"
        self.assertTrue(written.is_file())
        self.assertIn('base_url = "https://api.example.com/v1"', written.read_text())

    def test_dry_run_writes_nothing(self):
        self.add_provider()
        self.output(["start", "--provider", "explabs", "--dry-run"])
        self.assertFalse((self.codex_home / "explabs.config.toml").exists())

    def test_unknown_provider_reports_the_available_ones(self):
        code, _ = self.output(["start", "--provider", "nope", "--dry-run"])
        self.assertEqual(code, 2)

    def test_provider_is_rejected_for_claude(self):
        self.add_provider()
        code, _ = self.output(
            ["start", "--agent", "claude", "--provider", "explabs", "--dry-run"]
        )
        self.assertEqual(code, 2)

    def test_status_reports_the_active_provider(self):
        self.add_provider()
        self.output(["provider", "use", "explabs"])
        code, output = self.output(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Provider:  explabs", output)

    def test_show_renders_the_generated_profile(self):
        self.add_provider()
        code, output = self.output(["provider", "show", "explabs"])
        self.assertEqual(code, 0)
        self.assertIn("[model_providers.explabs]", output)
        self.assertIn('env_key = "EXAMPLE_API_KEY"', output)

    def test_remove_returns_the_workspace_to_the_default(self):
        self.add_provider()
        self.output(["provider", "use", "explabs"])
        code, output = self.output(["provider", "remove", "explabs"])
        self.assertEqual(code, 0)
        self.assertIn("Removed provider explabs", output)
        code, output = self.output(["start", "--dry-run"])
        self.assertNotIn("--profile explabs", output)

    def test_account_switching_still_works_under_a_provider(self):
        self.add_provider()
        self.add_account_2()
        self.output(["provider", "use", "explabs", "--global"])
        with patch("super_agent.cli.exec_command", return_value=0) as run:
            main(["2"])
        env = run.call_args.args[1]
        self.assertIn("--profile", run.call_args.args[0])
        self.assertTrue(
            (Path(env["CODEX_HOME"]) / "explabs.config.toml").is_file(),
            "the provider profile must exist in the account home being launched",
        )


class SessionsCliTests(unittest.TestCase):
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
            code, _ = self.output(["profile", "add", "codex", "2", "--label", "Personal"])
        self.assertEqual(code, 0)

    def account_home(self):
        store = Store(self.state)
        return store.profile_home("codex", "2", store.load(), create=True)

    def write_rollout(self, home, day, name):
        directory = Path(home) / "sessions" / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("transcript", encoding="utf-8")
        return path

    def write_archived_rollout(self, home, day, name):
        directory = Path(home) / "archived_sessions" / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("transcript", encoding="utf-8")
        return path

    def set_cutoff(self, since):
        store = Store(self.state)
        config = store.load()
        config["sessionSharing"]["since"] = since
        store.save(config)

    @patch("super_agent.cli.codex_set_thread_names")
    @patch("super_agent.cli.codex_thread_names")
    def test_session_names_use_the_most_recent_nonempty_name(self, listed, renamed):
        self.add_account_2()
        store = Store(self.state)
        config = store.load()
        account = self.account_home()

        def metadata(env):
            if env["CODEX_HOME"] == str(self.codex_home):
                return {
                    "main-only": {"name": "Main", "updatedAt": 10},
                    "account-newer": {"name": "Old", "updatedAt": 20},
                    "tie": {"name": "Main tie", "updatedAt": 30},
                }
            return {
                "main-only": {"name": None, "updatedAt": 15},
                "account-newer": {"name": "New", "updatedAt": 40},
                "tie": {"name": "Account tie", "updatedAt": 30},
            }

        changes = {}

        def apply(env, updates):
            changes[env["CODEX_HOME"]] = updates
            return len(updates)

        listed.side_effect = metadata
        renamed.side_effect = apply

        applied = reconcile_session_names(store, config)

        self.assertEqual(applied, 3)
        self.assertEqual(changes[str(self.codex_home)], {"account-newer": "New"})
        self.assertEqual(
            changes[str(account)], {"main-only": "Main", "tie": "Main tie"}
        )

    @patch("super_agent.cli.reconcile_session_names", return_value=0)
    def test_shared_profile_session_is_pushed_after_codex_exits(self, names):
        self.add_account_2()
        account = self.account_home()
        with patch("super_agent.cli.exec_command", return_value=0) as execute:
            code, _ = self.output(["start", "--profile", "2"])
        self.assertEqual(code, 0)
        after = execute.call_args.kwargs["after"]
        transcript = self.write_rollout(
            account, "2026/09/08", "rollout-2026-09-08T10-00-00-new.jsonl"
        )

        after()

        shared = self.codex_home / "sessions" / "2026" / "09" / "08" / transcript.name
        self.assertEqual(shared.stat().st_ino, transcript.stat().st_ino)
        names.assert_called_once()

    @patch("super_agent.cli.reconcile_session_names", return_value=0)
    def test_shared_store_session_is_pulled_after_codex_exits(self, names):
        self.add_account_2()
        account = self.account_home()
        with patch("super_agent.cli.exec_command", return_value=0) as execute:
            code, _ = self.output(["start", "--profile", "main"])
        self.assertEqual(code, 0)
        after = execute.call_args.kwargs["after"]
        transcript = self.write_rollout(
            self.codex_home,
            "2026/09/08",
            "rollout-2026-09-08T10-00-00-new.jsonl",
        )

        after()

        pulled = account / "sessions" / "2026" / "09" / "08" / transcript.name
        self.assertEqual(pulled.stat().st_ino, transcript.stat().st_ino)
        names.assert_called_once()

    def test_isolated_profile_does_not_schedule_post_exit_sharing(self):
        self.add_account_2()
        self.output(["sessions", "split", "--profile", "2"])
        with patch("super_agent.cli.exec_command", return_value=0) as execute:
            code, _ = self.output(["start", "--profile", "2"])

        self.assertEqual(code, 0)
        self.assertIsNone(execute.call_args.kwargs.get("after"))

    def test_status_reports_the_store_and_each_account(self):
        self.add_account_2()
        code, output = self.output(["sessions", "status"])
        self.assertEqual(code, 0)
        self.assertIn(str(self.codex_home), output)
        self.assertIn("shared by default", output)
        self.assertIn("Personal", output)

    def test_status_json_describes_sharing(self):
        self.add_account_2()
        code, output = self.output(["sessions", "status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["default"], "shared")
        self.assertEqual(payload["store"], str(self.codex_home))
        self.assertEqual(
            {row["profile"] for row in payload["accounts"]}, {"main", "2"}
        )

    def test_status_counts_the_backlog_a_cutoff_holds_back(self):
        self.add_account_2()
        self.account_home()
        self.set_cutoff("2026-09-06T00:00:00Z")
        self.write_rollout(
            self.codex_home, "2026/07/01", "rollout-2026-07-01T10-00-00-old.jsonl"
        )
        code, output = self.output(["sessions", "status"])
        self.assertEqual(code, 0)
        self.assertIn("1 not unified", output)
        self.assertIn("sc sessions merge --dry-run", output)

    def test_a_dry_run_merge_reports_without_writing(self):
        self.add_account_2()
        account = self.account_home()
        self.set_cutoff("2026-09-06T00:00:00Z")
        self.write_rollout(
            self.codex_home, "2026/07/01", "rollout-2026-07-01T10-00-00-old.jsonl"
        )
        code, output = self.output(["sessions", "merge", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("Would unify 1 transcripts", output)
        self.assertIn("Nothing was written", output)
        self.assertFalse((account / "sessions" / "2026" / "07").exists())

    def test_purge_previews_all_old_session_paths_without_deleting(self):
        self.add_account_2()
        account = self.account_home()
        session = self.write_rollout(
            self.codex_home, "2020/01/01", "rollout-2020-01-01T10-00-00-old.jsonl"
        )
        archived = self.write_archived_rollout(
            self.codex_home, "2020/01/02", "rollout-2020-01-02T10-00-00-archived.jsonl"
        )
        linked = account / "sessions" / "2020" / "01" / "01" / session.name
        linked.parent.mkdir(parents=True, exist_ok=True)
        os.link(session, linked)

        code, output = self.output(["sessions", "purge", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("Would permanently delete 3 transcript paths", output)
        self.assertIn("2 unique transcripts", output)
        self.assertIn("Reclaimable storage: 20 bytes", output)
        self.assertIn(str(self.codex_home / "sessions") + "/*", output)
        self.assertIn(str(self.codex_home / "archived_sessions") + "/*", output)
        self.assertIn(str(account / "sessions") + "/*", output)
        self.assertNotIn(str(session), output)
        self.assertNotIn(str(archived), output)
        self.assertNotIn(str(linked), output)
        self.assertTrue(session.exists())
        self.assertTrue(archived.exists())
        self.assertTrue(linked.exists())

    @patch("builtins.input", return_value="PURGE")
    def test_purge_requires_confirmation_and_removes_sessions_and_archives(self, confirm):
        session = self.write_rollout(
            self.codex_home, "2020/01/01", "rollout-2020-01-01T10-00-00-old.jsonl"
        )
        archived = self.write_archived_rollout(
            self.codex_home, "2020/01/02", "rollout-2020-01-02T10-00-00-archived.jsonl"
        )
        current = self.write_rollout(
            self.codex_home, "2099/01/01", "rollout-2099-01-01T10-00-00-new.jsonl"
        )

        code, output = self.output(["sessions", "purge"])

        self.assertEqual(code, 0)
        confirm.assert_called_once()
        self.assertIn("Permanently deleted 2 transcript paths", output)
        self.assertFalse(session.exists())
        self.assertFalse(archived.exists())
        self.assertTrue(current.exists())

    @patch("builtins.input", return_value="no")
    def test_purge_cancels_without_the_exact_confirmation(self, confirm):
        session = self.write_rollout(
            self.codex_home, "2020/01/01", "rollout-2020-01-01T10-00-00-old.jsonl"
        )

        code, output = self.output(["sessions", "purge"])

        self.assertEqual(code, 0)
        confirm.assert_called_once()
        self.assertIn("Purge cancelled", output)
        self.assertTrue(session.exists())

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_purge_ctrl_c_cancels_without_a_traceback(self, confirm):
        session = self.write_rollout(
            self.codex_home, "2020/01/01", "rollout-2020-01-01T10-00-00-old.jsonl"
        )

        code, output = self.output(["sessions", "purge"])

        self.assertEqual(code, 130)
        confirm.assert_called_once()
        self.assertIn("Purge cancelled", output)
        self.assertTrue(session.exists())

    def test_purge_rejects_a_non_positive_retention_window(self):
        code, _ = self.output(["sessions", "purge", "--older-than", "0"])
        self.assertEqual(code, 2)

    @patch("super_agent.cli.reconcile_session_names", return_value=0)
    def test_merge_unifies_the_backlog(self, names):
        self.add_account_2()
        account = self.account_home()
        self.set_cutoff("2026-09-06T00:00:00Z")
        transcript = self.write_rollout(
            self.codex_home, "2026/07/01", "rollout-2026-07-01T10-00-00-old.jsonl"
        )
        code, output = self.output(["sessions", "merge"])
        self.assertEqual(code, 0)
        self.assertIn("Unified 1 transcripts", output)
        linked = account / "sessions" / "2026" / "07" / "01" / transcript.name
        self.assertEqual(linked.stat().st_ino, transcript.stat().st_ino)
        self.assertIsNone(Store(self.state).load()["sessionSharing"]["since"])

    @patch("super_agent.cli.reconcile_session_names", return_value=2)
    def test_sync_reports_session_names_and_configuration(self, names):
        self.add_account_2()
        (self.codex_home / "config.toml").write_text("model = 'shared'\n", encoding="utf-8")

        code, output = self.output(["sessions", "sync"])

        self.assertEqual(code, 0)
        self.assertIn("synchronized 2 session names", output)
        self.assertIn("refreshed 1 configuration files", output)

    def test_split_and_share_toggle_one_account(self):
        self.add_account_2()
        code, output = self.output(["sessions", "split", "--profile", "2"])
        self.assertEqual(code, 0)
        self.assertIn("private to profile 2", output)
        store = Store(self.state)
        self.assertEqual(
            store.session_sharing_mode(store.load(), "2"), "isolated"
        )
        code, output = self.output(["sessions", "share", "--profile", "2"])
        self.assertEqual(code, 0)
        self.assertIn("Sharing session history", output)
        store = Store(self.state)
        self.assertEqual(store.session_sharing_mode(store.load(), "2"), "shared")

    def test_split_does_not_unlink_what_is_already_shared(self):
        self.add_account_2()
        account = self.account_home()
        transcript = self.write_rollout(
            self.codex_home, "2026/09/06", "rollout-2026-09-06T10-00-00-aaa.jsonl"
        )
        Store(self.state).environment("codex", "2", Store(self.state).load())
        linked = account / "sessions" / "2026" / "09" / "06" / transcript.name
        self.assertTrue(linked.exists())
        code, _ = self.output(["sessions", "split", "--profile", "2"])
        self.assertEqual(code, 0)
        self.assertTrue(linked.exists())

    def test_include_and_exclude_change_the_shared_groups(self):
        code, output = self.output(["sessions", "exclude", "config"])
        self.assertEqual(code, 0)
        self.assertNotIn("config", output.split(":", 1)[1])
        code, output = self.output(["sessions", "include", "config"])
        self.assertEqual(code, 0)
        self.assertIn("config", output)

    def test_an_unknown_group_is_refused_by_the_parser(self):
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                main(["sessions", "include", "secrets"])

    def test_status_names_where_the_active_account_records_sessions(self):
        code, output = self.output(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Sessions:", output)
        self.assertIn("shared with every account", output)

    def test_resume_adopts_a_session_recorded_by_another_account(self):
        self.add_account_2()
        account = self.account_home()
        self.output(["sessions", "split", "--profile", "2"])
        transcript = self.write_rollout(
            account, "2026/09/07", "rollout-2026-09-07T08-00-00-eeee.jsonl"
        )
        with patch("super_agent.cli.exec_command", return_value=0):
            code, output = self.output(["resume", "eeee", "--profile", "main"])
        self.assertEqual(code, 0)
        self.assertIn("Adopted", output)
        adopted = (
            self.codex_home / "sessions" / "2026" / "09" / "07" / transcript.name
        )
        self.assertEqual(adopted.stat().st_ino, transcript.stat().st_ino)

    def test_resume_leaves_a_known_session_alone(self):
        self.add_account_2()
        self.write_rollout(
            self.codex_home, "2026/09/07", "rollout-2026-09-07T08-00-00-ffff.jsonl"
        )
        with patch("super_agent.cli.exec_command", return_value=0):
            code, output = self.output(["resume", "ffff", "--profile", "main"])
        self.assertEqual(code, 0)
        self.assertNotIn("Adopted", output)
