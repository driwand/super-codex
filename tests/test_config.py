import json
import os
import stat
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from super_agent.config import (
    ConfigError,
    SHARED_CLAUDE_HOME_ENV,
    SHARED_CODEX_HOME_ENV,
    Store,
    default_config,
    ensure_private_directory,
    resolve_home,
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "state"
        self.codex_home = Path(self.temporary.name) / "main-codex"
        (self.codex_home / "sessions").mkdir(parents=True)
        (self.codex_home / "archived_sessions").mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.codex_home),
                SHARED_CODEX_HOME_ENV: str(self.codex_home),
            },
            clear=False,
        )
        self.environment.start()
        self.store = Store(self.home)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_first_load_creates_private_default_config(self):
        config = self.store.load()
        self.assertEqual(config["defaults"], {"agent": "codex", "profile": "main"})
        self.assertEqual(config["version"], 2)
        self.assertEqual(config["startupMode"], "select")
        self.assertEqual(config["profileOrder"]["codex"], ["main"])
        mode = stat.S_IMODE(self.store.config_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_default_state_root_uses_new_product_name(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temporary.name}, clear=True):
            self.assertEqual(resolve_home(), Path(self.temporary.name) / "super-codex")

    def test_default_state_root_ignores_old_product_directory(self):
        (Path(self.temporary.name) / "super-agent-control").mkdir()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temporary.name}, clear=True):
            self.assertEqual(resolve_home(), Path(self.temporary.name) / "super-codex")

    def test_isolated_profile_uses_private_provider_home(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        with patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True):
            env = self.store.environment("codex", "2", config)
        expected = (self.home / "profiles" / "codex" / "2").absolute()
        self.assertEqual(env["CODEX_HOME"], str(expected))
        self.assertTrue(expected.is_dir())
        self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o700)

    def test_isolated_codex_profile_uses_real_session_directories(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        env = self.store.environment("codex", "2", config)
        profile_home = Path(env["CODEX_HOME"])
        for name in ("sessions", "archived_sessions"):
            directory = profile_home / name
            self.assertFalse(directory.is_symlink())
            self.assertTrue(directory.is_dir())
        self.assertFalse((profile_home / "auth.json").exists())

    def test_migrates_linked_session_directories_into_real_directories(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        profile_home = self.store.profile_home("codex", "2", config, create=True)
        shared_rollout = self.codex_home / "sessions" / "2026" / "08"
        shared_rollout.mkdir(parents=True)
        transcript = shared_rollout / "rollout-2026-08-20T00-00-00-abc.jsonl"
        transcript.write_text("shared", encoding="utf-8")
        for name in ("sessions", "archived_sessions"):
            (profile_home / name).symlink_to(
                self.codex_home / name, target_is_directory=True
            )

        with patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True):
            self.store.environment("codex", "2", config)

        migrated = profile_home / "sessions"
        self.assertFalse(migrated.is_symlink())
        self.assertTrue(migrated.is_dir())
        linked = migrated / "2026" / "08" / transcript.name
        self.assertEqual(linked.read_text(encoding="utf-8"), "shared")
        self.assertEqual(linked.stat().st_ino, transcript.stat().st_ino)
        self.assertFalse((profile_home / "archived_sessions").is_symlink())
        self.assertEqual(list(profile_home.glob("sessions.*.migrating")), [])

    def test_migration_keeps_transcripts_written_by_the_profile(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        profile_home = self.store.profile_home("codex", "2", config, create=True)
        sessions = profile_home / "sessions"
        sessions.mkdir()
        (sessions / "existing-session.jsonl").write_text("local", encoding="utf-8")

        self.store.environment("codex", "2", config)

        self.assertEqual(
            (sessions / "existing-session.jsonl").read_text(encoding="utf-8"), "local"
        )

    def test_rejects_session_link_outside_the_shared_home(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        profile_home = self.store.profile_home("codex", "2", config, create=True)
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        (profile_home / "sessions").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, "unexpected Codex session link"):
            self.store.environment("codex", "2", config)

    def test_replacement_home_keeps_the_profile_transcripts(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        profile_home = self.store.profile_home("codex", "2", config, create=True)
        sessions = profile_home / "sessions"
        sessions.mkdir()
        transcript = sessions / "rollout-2026-08-20T00-00-00-abc.jsonl"
        transcript.write_text("profile", encoding="utf-8")

        provider_home, env = self.store.replacement_environment("codex", "2", config)
        candidate = Path(env["CODEX_HOME"])

        self.assertEqual(candidate.name, provider_home)
        carried = candidate / "sessions" / transcript.name
        self.assertEqual(carried.read_text(encoding="utf-8"), "profile")
        self.assertEqual(carried.stat().st_ino, transcript.stat().st_ino)
        self.assertFalse((candidate / "sessions").is_symlink())

    def test_shared_profiles_preserve_exported_provider_homes(self):
        config = self.store.load()
        with patch.dict(
            os.environ,
            {"CODEX_HOME": "/inherited", "CLAUDE_CONFIG_DIR": "/inherited-claude"},
            clear=True,
        ):
            env = self.store.environment("codex", "main", config)
            claude_env = self.store.environment("claude", "main", config)
        self.assertEqual(env["CODEX_HOME"], "/inherited")
        self.assertEqual(env[SHARED_CODEX_HOME_ENV], "/inherited")
        self.assertEqual(claude_env["CLAUDE_CONFIG_DIR"], "/inherited-claude")

    def test_nested_shared_codex_restores_exported_home(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        with patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
        ):
            isolated = self.store.environment("codex", "2", config)
        with patch.dict(os.environ, isolated, clear=True):
            shared = self.store.environment("codex", "main", config)
        self.assertEqual(shared["CODEX_HOME"], str(self.codex_home.absolute()))
        self.assertEqual(
            shared[SHARED_CODEX_HOME_ENV], str(self.codex_home.absolute())
        )

    def test_nested_shared_claude_restores_exported_home(self):
        config = self.store.load()
        self.store.add_profile(config, "claude", "reviewer", "Reviewer")
        exported = str(Path(self.temporary.name) / "shared-claude")
        with patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": exported}, clear=True
        ):
            isolated = self.store.environment("claude", "reviewer", config)
        self.assertEqual(isolated[SHARED_CLAUDE_HOME_ENV], exported)
        with patch.dict(os.environ, isolated, clear=True):
            shared = self.store.environment("claude", "main", config)
        self.assertEqual(shared["CLAUDE_CONFIG_DIR"], exported)

    def test_nested_shared_claude_restores_unset_home(self):
        config = self.store.load()
        self.store.add_profile(config, "claude", "reviewer", "Reviewer")
        with patch.dict(os.environ, {}, clear=True):
            isolated = self.store.environment("claude", "reviewer", config)
        self.assertEqual(isolated[SHARED_CLAUDE_HOME_ENV], "")
        with patch.dict(os.environ, isolated, clear=True):
            shared = self.store.environment("claude", "main", config)
        self.assertNotIn("CLAUDE_CONFIG_DIR", shared)

    def test_claude_replacement_environment_remembers_shared_home(self):
        config = self.store.load()
        self.store.add_profile(config, "claude", "reviewer", "Reviewer")
        exported = str(Path(self.temporary.name) / "shared-claude")
        with patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": exported}, clear=True
        ):
            _, replacement = self.store.replacement_environment(
                "claude", "reviewer", config
            )
        self.assertEqual(replacement[SHARED_CLAUDE_HOME_ENV], exported)
        self.assertNotEqual(replacement["CLAUDE_CONFIG_DIR"], exported)

    def test_nearest_workspace_binding_wins(self):
        config = self.store.load()
        project = Path(self.temporary.name) / "project"
        child = project / "src" / "feature"
        child.mkdir(parents=True)
        self.store.add_profile(config, "codex", "2", "Personal")
        self.store.bind(config, project, "codex", "2")
        agent, profile, matched = self.store.selection(config, child)
        self.assertEqual((agent, profile), ("codex", "2"))
        self.assertEqual(matched, str(project.resolve()))

    def test_agent_override_uses_that_agents_default_profile(self):
        config = self.store.load()
        agent, profile, _ = self.store.selection(config, self.temporary.name, agent="claude")
        self.assertEqual((agent, profile), ("claude", "main"))

    def test_add_profile_does_not_create_credential_files(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Client")
        profile_home = self.home / "profiles" / "codex" / "2"
        self.assertEqual(list(profile_home.iterdir()), [])
        loaded = self.store.load()
        self.assertEqual(loaded["profiles"]["codex"]["2"]["label"], "Client")
        self.assertNotIn("routing", loaded)

    def test_global_binding_updates_defaults(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        self.store.bind(config, self.temporary.name, "codex", "2", globally=True)
        loaded = self.store.load()
        self.assertEqual(loaded["defaults"], {"agent": "codex", "profile": "2"})
        self.assertEqual(loaded["agentDefaults"]["codex"], "2")

    def test_main_profile_updates_defaults_without_changing_profile_storage(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        profiles = json.loads(json.dumps(config["profiles"]))
        self.store.set_main_profile(config, "codex", "2")
        loaded = self.store.load()
        self.assertEqual(loaded["defaults"], {"agent": "codex", "profile": "2"})
        self.assertEqual(loaded["agentDefaults"]["codex"], "2")
        self.assertEqual(loaded["profiles"], profiles)

    def test_main_profile_for_inactive_agent_preserves_global_agent(self):
        config = self.store.load()
        self.store.add_profile(config, "claude", "reviewer", "Reviewer")
        self.store.set_main_profile(config, "claude", "reviewer")
        loaded = self.store.load()
        self.assertEqual(loaded["defaults"], {"agent": "codex", "profile": "main"})
        self.assertEqual(loaded["agentDefaults"]["claude"], "reviewer")

    def test_main_profile_must_exist(self):
        config = self.store.load()
        with self.assertRaisesRegex(ConfigError, "Unknown profile"):
            self.store.set_main_profile(config, "codex", "2")

    def test_missing_agent_default_is_rejected(self):
        config = default_config()
        del config["agentDefaults"]["codex"]
        with self.assertRaises(ConfigError):
            self.store.validate(config)

    def test_malformed_defaults_are_rejected_as_config_errors(self):
        config = default_config()
        config["defaults"] = None
        with self.assertRaisesRegex(ConfigError, "defaults must be an object"):
            self.store.validate(config)

        config = default_config()
        config["agentDefaults"]["codex"] = []
        with self.assertRaisesRegex(ConfigError, "agentDefaults.codex must be a string"):
            self.store.validate(config)

    def test_profile_label_can_be_changed(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        self.store.set_label(config, "codex", "2", "Work")
        self.assertEqual(self.store.load()["profiles"]["codex"]["2"]["label"], "Work")

    def test_rejects_symlinked_home(self):
        target = Path(self.temporary.name) / "target-home"
        target.mkdir()
        linked_home = Path(self.temporary.name) / "linked-home"
        linked_home.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ConfigError):
            Store(linked_home).load()

    def test_rejects_symlinked_profile_directory(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        profile_parent = self.home / "profiles" / "codex"
        profile_parent.mkdir(parents=True, exist_ok=True)
        target = Path(self.temporary.name) / "target-profile"
        target.mkdir()
        (profile_parent / "2").rmdir()
        (profile_parent / "2").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.store.environment("codex", "2", config)

    def test_environment_copy_does_not_mutate_parent(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2", "Personal")
        with patch.dict(
            os.environ,
            {"PARENT_ONLY": "yes", "CODEX_HOME": str(self.codex_home)},
            clear=True,
        ):
            env = self.store.environment("codex", "2", config)
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

    def test_codex_profiles_are_limited_to_main_and_numbers_through_five(self):
        config = self.store.load()
        for name in ("2", "3", "4", "5"):
            self.store.add_profile(config, "codex", name)
        self.assertEqual(self.store.ordered_profile_names(config, "codex"), ["main", "2", "3", "4", "5"])
        with self.assertRaisesRegex(ConfigError, "named main, 2, 3, 4, or 5"):
            self.store.add_profile(config, "codex", "6")

    def test_profile_order_is_explicit_and_persisted(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "2")
        self.store.add_profile(config, "codex", "3")
        self.store.set_profile_order(config, "codex", ["3", "main", "2"])
        self.assertEqual(self.store.load()["profileOrder"]["codex"], ["3", "main", "2"])
        with self.assertRaisesRegex(ConfigError, "list every codex profile once"):
            self.store.set_profile_order(config, "codex", ["main", "2"])

    def test_numbered_profiles_do_not_need_to_be_sequential(self):
        config = self.store.load()
        self.store.add_profile(config, "codex", "5", "Fifth account")
        self.assertEqual(self.store.ordered_profile_names(config, "codex"), ["main", "5"])
        self.assertIsNotNone(self.store.profile_home("codex", "5", config))

    def test_startup_mode_is_global_and_validated(self):
        config = self.store.load()
        self.store.set_startup_mode(config, "main")
        self.assertEqual(self.store.load()["startupMode"], "main")
        with self.assertRaisesRegex(ConfigError, "Startup mode"):
            self.store.set_startup_mode(config, "automatic")

    def test_schema_v1_is_rejected_without_legacy_migration(self):
        config = default_config()
        config["version"] = 1
        with self.assertRaisesRegex(ConfigError, str(self.store.config_path)):
            self.store.save(config)

    def test_nested_private_directories_are_created_with_private_modes(self):
        existing = Path(self.temporary.name) / "existing"
        existing.mkdir(mode=0o755)
        nested = existing / "runtime" / "codex" / "main"
        ensure_private_directory(nested)
        for path in (existing / "runtime", existing / "runtime" / "codex", nested):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
