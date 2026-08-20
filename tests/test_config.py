import json
import os
import stat
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from super_agent.config import ConfigError, Store, default_config, resolve_home


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "state"
        self.codex_home = Path(self.temporary.name) / "main-codex"
        (self.codex_home / "sessions").mkdir(parents=True)
        (self.codex_home / "archived_sessions").mkdir()
        self.environment = patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=False
        )
        self.environment.start()
        self.store = Store(self.home)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_first_load_creates_private_default_config(self):
        config = self.store.load()
        self.assertEqual(config["defaults"], {"agent": "codex", "profile": "main"})
        self.assertIn("second", config["profiles"]["codex"])
        mode = stat.S_IMODE(self.store.config_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_default_state_root_uses_new_product_name(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temporary.name}, clear=True):
            self.assertEqual(resolve_home(), Path(self.temporary.name) / "super-codex")

    def test_isolated_profile_uses_private_provider_home(self):
        config = self.store.load()
        with patch.dict(os.environ, {}, clear=True):
            env = self.store.environment("codex", "second", config)
        expected = (self.home / "profiles" / "codex" / "second").absolute()
        self.assertEqual(env["CODEX_HOME"], str(expected))
        self.assertTrue(expected.is_dir())
        self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o700)

    def test_isolated_codex_profile_shares_only_session_directories(self):
        config = self.store.load()
        env = self.store.environment("codex", "second", config)
        profile_home = Path(env["CODEX_HOME"])
        for name in ("sessions", "archived_sessions"):
            linked = profile_home / name
            self.assertTrue(linked.is_symlink())
            self.assertEqual(linked.resolve(), (self.codex_home / name).resolve())
        self.assertFalse((profile_home / "auth.json").exists())

    def test_refuses_to_replace_existing_isolated_session_data(self):
        config = self.store.load()
        profile_home = self.store.profile_home("codex", "second", config, create=True)
        sessions = profile_home / "sessions"
        sessions.mkdir()
        (sessions / "existing-session.jsonl").write_text("local", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "already contains data"):
            self.store.environment("codex", "second", config)

    def test_shared_profile_inherits_provider_environment(self):
        config = self.store.load()
        with patch.dict(os.environ, {"CODEX_HOME": "/inherited"}, clear=True):
            env = self.store.environment("codex", "main", config)
        self.assertEqual(env["CODEX_HOME"], "/inherited")

    def test_nearest_workspace_binding_wins(self):
        config = self.store.load()
        project = Path(self.temporary.name) / "project"
        child = project / "src" / "feature"
        child.mkdir(parents=True)
        self.store.bind(config, project, "codex", "second")
        agent, profile, matched = self.store.selection(config, child)
        self.assertEqual((agent, profile), ("codex", "second"))
        self.assertEqual(matched, str(project.resolve()))

    def test_agent_override_uses_that_agents_default_profile(self):
        config = self.store.load()
        agent, profile, _ = self.store.selection(config, self.temporary.name, agent="claude")
        self.assertEqual((agent, profile), ("claude", "main"))

    def test_add_profile_does_not_create_credential_files(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "client", "Client")
        profile_home = self.home / "profiles" / "codex" / "client"
        self.assertEqual(list(profile_home.iterdir()), [])
        loaded = self.store.load()
        self.assertEqual(loaded["profiles"]["codex"]["client"]["label"], "Client")
        self.assertNotIn("routing", loaded)

    def test_global_binding_updates_defaults(self):
        config = self.store.load()
        self.store.bind(config, self.temporary.name, "codex", "second", globally=True)
        loaded = self.store.load()
        self.assertEqual(loaded["defaults"], {"agent": "codex", "profile": "second"})
        self.assertEqual(loaded["agentDefaults"]["codex"], "second")

    def test_missing_agent_default_is_rejected(self):
        config = default_config()
        del config["agentDefaults"]["codex"]
        with self.assertRaises(ConfigError):
            self.store.validate(config)

    def test_profile_label_can_be_changed(self):
        config = self.store.load()
        self.store.set_label(config, "codex", "second", "Work")
        self.assertEqual(self.store.load()["profiles"]["codex"]["second"]["label"], "Work")

    def test_rejects_symlinked_home(self):
        target = Path(self.temporary.name) / "target-home"
        target.mkdir()
        linked_home = Path(self.temporary.name) / "linked-home"
        linked_home.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ConfigError):
            Store(linked_home).load()

    def test_rejects_symlinked_profile_directory(self):
        config = self.store.load()
        profile_parent = self.home / "profiles" / "codex"
        profile_parent.mkdir(parents=True)
        target = Path(self.temporary.name) / "target-profile"
        target.mkdir()
        (profile_parent / "second").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.store.environment("codex", "second", config)

    def test_environment_copy_does_not_mutate_parent(self):
        config = self.store.load()
        with patch.dict(os.environ, {"PARENT_ONLY": "yes"}, clear=True):
            env = self.store.environment("codex", "second", config)
            env["PARENT_ONLY"] = "changed"
            self.assertEqual(os.environ["PARENT_ONLY"], "yes")

    def test_no_workspace_binding_uses_global_default(self):
        config = self.store.load()
        agent, profile, matched = self.store.selection(config, self.temporary.name)
        self.assertEqual((agent, profile, matched), ("codex", "main", None))

    def test_rejects_symlinked_config(self):
        self.store._ensure_home()
        target = self.home / "target.json"
        target.write_text(json.dumps(default_config()), encoding="utf-8")
        self.store.config_path.symlink_to(target)
        with self.assertRaises(ConfigError):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
