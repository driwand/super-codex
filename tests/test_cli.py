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

from super_agent.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.environment = patch.dict(
            os.environ, {"SUPER_AGENT_HOME": str(self.state)}, clear=False
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

    def test_default_dry_run_is_codex_main(self):
        code, output = self.output(["start", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("codex -C", output)
        self.assertNotIn("CODEX_HOME=", output)

    def test_second_codex_profile_has_isolated_home(self):
        code, output = self.output(
            ["start", "--agent", "codex", "--profile", "second", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIn("CODEX_HOME=", output)
        self.assertIn("profiles/codex/second", output)

    def test_workspace_binding_changes_default_launch(self):
        code, _ = self.output(["use", "codex", "second"])
        self.assertEqual(code, 0)
        code, output = self.output(["start", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("CODEX_HOME=", output)

    def test_explicit_claude_override_uses_claude_default(self):
        code, output = self.output(["start", "--agent", "claude", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("claude", output)

    @patch("super_agent.cli.profile_rows", return_value=[])
    def test_status_identifies_active_binding(self, profile_rows):
        self.output(["use", "codex", "second"])
        code, output = self.output(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Active:    codex/second", output)

    @patch("super_agent.cli.profile_rows", return_value=[])
    def test_usage_all_targets_both_codex_profiles(self, profile_rows):
        code, _ = self.output(["usage", "--all"])
        self.assertEqual(code, 0)
        targets = profile_rows.call_args.kwargs["only"]
        self.assertEqual(targets, {("codex", "main"), ("codex", "second")})

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
        self.output(["use", "codex", "second"])
        code, output = self.output(["bindings"])
        self.assertEqual(code, 0)
        self.assertIn("-> codex/second", output)
        code, output = self.output(["bindings", "--json"])
        self.assertEqual(json.loads(output)["bindings"][0]["profile"], "second")

    def test_profile_label_command(self):
        code, output = self.output(["profile", "label", "codex", "second", "Work"])
        self.assertEqual(code, 0)
        self.assertIn("Labeled codex/second as Work", output)
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["profiles"]["codex"]["second"]["label"], "Work")

    @patch("super_agent.cli.executable", return_value="/bin/agent")
    @patch("super_agent.cli.profile_rows")
    def test_setup_gives_second_account_next_step(self, profile_rows, executable):
        profile_rows.return_value = [
            {"agent": "codex", "profile": "main", "label": "Codex 1", "isolation": "shared", "authenticated": True, "authDetail": "ready", "live": []},
            {"agent": "codex", "profile": "second", "label": "Codex 2", "isolation": "isolated", "authenticated": False, "authDetail": "not logged in", "live": []},
            {"agent": "claude", "profile": "main", "label": "Claude", "isolation": "shared", "authenticated": True, "authDetail": "ready", "live": []},
        ]
        code, output = self.output(["setup"])
        self.assertEqual(code, 0)
        self.assertIn("sc login --profile second", output)

    def test_config_contains_no_credentials(self):
        self.output(["start", "--dry-run"])
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        serialized = json.dumps(config).lower()
        self.assertNotIn("token", serialized)
        self.assertNotIn("credential", serialized)
        self.assertNotIn("api_key", serialized)


if __name__ == "__main__":
    unittest.main()
