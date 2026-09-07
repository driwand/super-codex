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
    DEFAULT_PROVIDER,
    SHARING_GROUPS,
    ConfigError,
    SHARED_CLAUDE_HOME_ENV,
    SHARED_CODEX_HOME_ENV,
    Store,
    default_config,
    ensure_private_directory,
    render_codex_provider_profile,
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


class ProviderTests(unittest.TestCase):
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
        self.config = self.store.load()
        self.workspace = str(Path(self.temporary.name).resolve())

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def add(self, name="explabs", **overrides):
        options = {
            "base_url": "https://api.example.com/v1",
            "env_key": "EXAMPLE_API_KEY",
            "model": "example-model",
            "label": "Example",
        }
        options.update(overrides)
        return self.store.add_provider(self.config, name, **options)

    def test_default_configuration_has_no_providers(self):
        self.assertEqual(self.config["providers"], {})
        self.assertEqual(self.config["providerDefaults"]["codex"], DEFAULT_PROVIDER)

    def test_add_and_reload_provider(self):
        self.add()
        reloaded = Store(self.home).load()
        self.assertEqual(reloaded["providers"]["explabs"]["baseUrl"], "https://api.example.com/v1")
        self.assertEqual(reloaded["providers"]["explabs"]["wireApi"], "responses")

    def test_reserved_default_name_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.add(DEFAULT_PROVIDER)

    def test_invalid_provider_fields_are_rejected(self):
        with self.assertRaises(ConfigError):
            self.add(base_url="ftp://example.com")
        with self.assertRaises(ConfigError):
            self.add(env_key="lowercase_key")
        with self.assertRaises(ConfigError):
            self.add(model="")
        with self.assertRaises(ConfigError):
            self.add(wire_api="grpc")

    def test_selection_prefers_flag_then_binding_then_default(self):
        self.add()
        self.assertEqual(
            self.store.provider_selection(self.config, self.workspace)[0], DEFAULT_PROVIDER
        )
        self.store.bind_provider(self.config, self.workspace, "explabs")
        self.assertEqual(
            self.store.provider_selection(self.config, self.workspace)[0], "explabs"
        )
        self.assertEqual(
            self.store.provider_selection(self.config, self.workspace, DEFAULT_PROVIDER)[0],
            DEFAULT_PROVIDER,
        )

    def test_binding_a_provider_does_not_pin_the_account(self):
        self.add()
        self.store.bind_provider(self.config, self.workspace, "explabs")
        binding, _ = self.store.workspace_binding(self.config, self.workspace)
        self.assertIsNone(binding)
        self.assertEqual(
            self.store.provider_selection(self.config, self.workspace)[0], "explabs"
        )

    def test_account_and_provider_bindings_are_independent(self):
        self.add()
        self.store.bind(self.config, self.workspace, "codex", "main")
        self.store.bind_provider(self.config, self.workspace, "explabs")
        self.store.bind(self.config, self.workspace, "codex", "main")
        self.assertEqual(
            self.store.provider_selection(self.config, self.workspace)[0], "explabs"
        )
        self.store.bind_provider(self.config, self.workspace, DEFAULT_PROVIDER)
        self.assertEqual(
            self.store.provider_selection(self.config, self.workspace)[0], DEFAULT_PROVIDER
        )
        self.assertEqual(
            self.store.selection(self.config, self.workspace)[1], "main"
        )

    def test_provider_binding_is_inherited_by_subdirectories(self):
        self.add()
        child = Path(self.workspace) / "nested" / "deeper"
        child.mkdir(parents=True)
        self.store.bind_provider(self.config, self.workspace, "explabs")
        self.assertEqual(
            self.store.provider_selection(self.config, str(child))[0], "explabs"
        )

    def test_unbind_releases_the_provider_binding(self):
        self.add()
        self.store.bind_provider(self.config, self.workspace, "explabs")
        self.assertTrue(self.store.unbind(self.config, self.workspace))
        self.assertEqual(
            self.store.provider_selection(self.config, self.workspace)[0], DEFAULT_PROVIDER
        )

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.store.provider_selection(self.config, self.workspace, "missing")

    def test_removing_a_provider_releases_its_bindings(self):
        self.add()
        self.store.bind_provider(self.config, self.workspace, "explabs")
        self.store.bind_provider(self.config, self.workspace, "explabs", globally=True)
        _, unbound = self.store.remove_provider(self.config, "explabs")
        self.assertEqual(len(unbound), 1)
        self.assertEqual(self.config["providerDefaults"]["codex"], DEFAULT_PROVIDER)
        self.assertEqual(
            self.store.provider_selection(self.config, self.workspace)[0], DEFAULT_PROVIDER
        )

    def test_materialize_writes_a_private_profile_into_the_codex_home(self):
        self.add(reasoning="medium")
        env = self.store.environment("codex", "main", self.config)
        path = self.store.materialize_codex_provider(env, self.config, "explabs")
        self.assertEqual(path, self.codex_home / "explabs.config.toml")
        content = path.read_text(encoding="utf-8")
        self.assertIn('model_provider = "explabs"', content)
        self.assertIn("[model_providers.explabs]", content)
        self.assertIn('env_key = "EXAMPLE_API_KEY"', content)
        self.assertIn('model_reasoning_effort = "medium"', content)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_materialize_never_writes_a_credential(self):
        self.add()
        with patch.dict(os.environ, {"EXAMPLE_API_KEY": "secret-value"}, clear=False):
            env = self.store.environment("codex", "main", self.config)
            path = self.store.materialize_codex_provider(env, self.config, "explabs")
        self.assertNotIn("secret-value", path.read_text(encoding="utf-8"))

    def test_materialize_targets_the_isolated_home_of_the_account(self):
        self.add()
        self.store.add_profile(self.config, "codex", "2", label="Second")
        env = self.store.environment("codex", "2", self.config)
        path = self.store.materialize_codex_provider(env, self.config, "explabs")
        self.assertEqual(Path(env["CODEX_HOME"]), path.parent)
        self.assertNotEqual(path.parent, self.codex_home)

    def test_default_provider_materializes_nothing(self):
        env = self.store.environment("codex", "main", self.config)
        self.assertIsNone(
            self.store.materialize_codex_provider(env, self.config, DEFAULT_PROVIDER)
        )

    def test_provider_does_not_change_the_session_home(self):
        self.add()
        without = self.store.environment("codex", "main", self.config)["CODEX_HOME"]
        self.store.bind_provider(self.config, self.workspace, "explabs")
        with_provider = self.store.environment("codex", "main", self.config)["CODEX_HOME"]
        self.assertEqual(without, with_provider)

    def test_configuration_written_before_providers_still_loads(self):
        legacy = default_config()
        del legacy["providers"]
        del legacy["providerDefaults"]
        self.store.config_path.write_text(json.dumps(legacy), encoding="utf-8")
        os.chmod(self.store.config_path, 0o600)
        migrated = Store(self.home).load()
        self.assertEqual(migrated["providers"], {})
        self.assertEqual(migrated["providerDefaults"]["codex"], DEFAULT_PROVIDER)

    def test_rendered_profile_escapes_quotes(self):
        rendered = render_codex_provider_profile(
            "x", {"label": 'a "quoted" label', "model": "m", "baseUrl": "https://e/v1",
                  "envKey": "K", "wireApi": "responses"}
        )
        self.assertIn('name = "a \\"quoted\\" label"', rendered)


class SessionSharingTests(unittest.TestCase):
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
        self.config = self.store.load()
        self.store.add_profile(self.config, "codex", "2", "Personal")

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def account_home(self):
        return self.store.profile_home("codex", "2", self.config, create=True)

    def write_rollout(self, home, day, name, text="transcript"):
        directory = Path(home) / "sessions" / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_new_configurations_share_every_group_without_a_cutoff(self):
        sharing = self.config["sessionSharing"]
        self.assertEqual(sharing["default"], "shared")
        self.assertIsNone(sharing["since"])
        self.assertEqual(sharing["include"], list(SHARING_GROUPS))

    def test_transcripts_reach_both_homes_as_one_inode(self):
        shared = self.write_rollout(
            self.codex_home, "2026/09/06", "rollout-2026-09-06T10-00-00-aaa.jsonl"
        )
        account = self.account_home()
        own = self.write_rollout(
            account, "2026/09/06", "rollout-2026-09-06T11-00-00-bbb.jsonl"
        )

        self.store.environment("codex", "2", self.config)

        pulled = account / "sessions" / "2026" / "09" / "06" / shared.name
        pushed = self.codex_home / "sessions" / "2026" / "09" / "06" / own.name
        self.assertEqual(pulled.stat().st_ino, shared.stat().st_ino)
        self.assertEqual(pushed.stat().st_ino, own.stat().st_ino)

    def test_transcript_sharing_refuses_to_create_a_diverging_copy(self):
        shared = self.write_rollout(
            self.codex_home, "2026/09/06", "rollout-2026-09-06T10-00-00-aaa.jsonl"
        )
        account = self.account_home()

        with patch("super_agent.config.os.link", side_effect=OSError("cross-device")):
            with self.assertRaisesRegex(ConfigError, "same filesystem"):
                self.store._link_session_tree(
                    self.codex_home / "sessions", account / "sessions"
                )

        destination = account / "sessions" / "2026" / "09" / "06" / shared.name
        self.assertFalse(destination.exists())

    def test_transcript_sharing_refuses_an_existing_conflicting_file(self):
        shared = self.write_rollout(
            self.codex_home,
            "2026/09/06",
            "rollout-2026-09-06T10-00-00-aaa.jsonl",
            "shared",
        )
        account = self.account_home()
        conflicting = self.write_rollout(
            account, "2026/09/06", shared.name, "account"
        )

        with self.assertRaisesRegex(ConfigError, "conflicting shared Codex asset"):
            self.store._link_session_tree(
                self.codex_home / "sessions", account / "sessions"
            )

        self.assertEqual(conflicting.read_text(encoding="utf-8"), "account")

    def test_reconciling_twice_changes_nothing(self):
        shared = self.write_rollout(
            self.codex_home, "2026/09/06", "rollout-2026-09-06T10-00-00-aaa.jsonl"
        )
        self.store.environment("codex", "2", self.config)
        first = self.store.reconcile_sessions(self.config)
        self.assertEqual(sum(r["pulled"] + r["pushed"] for r in first), 0)
        account = self.account_home()
        self.assertEqual(
            (account / "sessions" / "2026" / "09" / "06" / shared.name).stat().st_ino,
            shared.stat().st_ino,
        )

    def test_a_cutoff_holds_back_transcripts_recorded_before_it(self):
        self.config["sessionSharing"]["since"] = "2026-09-06T00:00:00Z"
        self.store.save(self.config)
        backlog = self.write_rollout(
            self.codex_home, "2026/07/01", "rollout-2026-07-01T10-00-00-old.jsonl"
        )
        recent = self.write_rollout(
            self.codex_home, "2026/09/07", "rollout-2026-09-07T10-00-00-new.jsonl"
        )

        account = self.account_home()
        self.store.environment("codex", "2", self.config)

        self.assertFalse((account / "sessions" / "2026" / "07").exists())
        self.assertTrue(
            (account / "sessions" / "2026" / "09" / "07" / recent.name).exists()
        )
        self.assertTrue(backlog.exists())

    def test_merging_unifies_the_backlog_and_retires_the_cutoff(self):
        self.config["sessionSharing"]["since"] = "2026-09-06T00:00:00Z"
        self.store.save(self.config)
        backlog = self.write_rollout(
            self.codex_home, "2026/07/01", "rollout-2026-07-01T10-00-00-old.jsonl"
        )
        account = self.account_home()
        own = self.write_rollout(
            account, "2026/06/01", "rollout-2026-06-01T10-00-00-mine.jsonl"
        )

        reports = self.store.merge_sessions(self.config)

        self.assertEqual(sum(r["pulled"] + r["pushed"] for r in reports), 2)
        self.assertTrue(
            (account / "sessions" / "2026" / "07" / "01" / backlog.name).exists()
        )
        self.assertTrue(
            (self.codex_home / "sessions" / "2026" / "06" / "01" / own.name).exists()
        )
        self.assertIsNone(self.store.load()["sessionSharing"]["since"])

    def test_a_dry_run_merge_writes_nothing(self):
        self.write_rollout(
            self.codex_home, "2026/09/06", "rollout-2026-09-06T10-00-00-aaa.jsonl"
        )
        account = self.account_home()

        reports = self.store.merge_sessions(self.config, dry_run=True)

        self.assertEqual(sum(r["pulled"] + r["pushed"] for r in reports), 1)
        self.assertFalse((account / "sessions" / "2026").exists())

    def test_an_isolated_account_keeps_its_transcripts_to_itself(self):
        self.store.set_session_sharing(self.config, "isolated", "2")
        shared = self.write_rollout(
            self.codex_home, "2026/09/06", "rollout-2026-09-06T10-00-00-aaa.jsonl"
        )
        account = self.account_home()
        own = self.write_rollout(
            account, "2026/09/06", "rollout-2026-09-06T11-00-00-bbb.jsonl"
        )

        self.store.environment("codex", "2", self.config)

        self.assertFalse(
            (account / "sessions" / "2026" / "09" / "06" / shared.name).exists()
        )
        self.assertFalse(
            (self.codex_home / "sessions" / "2026" / "09" / "06" / own.name).exists()
        )

    def test_sharing_never_moves_a_credential_or_a_local_index(self):
        (self.codex_home / "auth.json").write_text("shared-account", encoding="utf-8")
        (self.codex_home / "installation_id").write_text("main", encoding="utf-8")
        (self.codex_home / "state_5.sqlite").write_text("index", encoding="utf-8")
        (self.codex_home / "version.json").write_text("{}", encoding="utf-8")
        account = self.account_home()
        (account / "auth.json").write_text("second-account", encoding="utf-8")

        self.store.environment("codex", "2", self.config)

        self.assertEqual(
            (account / "auth.json").read_text(encoding="utf-8"), "second-account"
        )
        for name in ("installation_id", "state_5.sqlite", "version.json"):
            self.assertFalse((account / name).exists(), name)

    def test_configuration_follows_the_store_and_backs_up_what_it_replaces(self):
        (self.codex_home / "config.toml").write_text('model = "new"\n', encoding="utf-8")
        (self.codex_home / "explabs.config.toml").write_text("x = 1\n", encoding="utf-8")
        account = self.account_home()
        (account / "config.toml").write_text('model = "old"\n', encoding="utf-8")

        self.store.environment("codex", "2", self.config)

        self.assertEqual(
            (account / "config.toml").read_text(encoding="utf-8"), 'model = "new"\n'
        )
        self.assertEqual(
            (account / "explabs.config.toml").read_text(encoding="utf-8"), "x = 1\n"
        )
        backups = list(account.glob("config.toml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), 'model = "old"\n')

    def test_configuration_breaks_an_existing_hard_link(self):
        source = self.codex_home / "config.toml"
        source.write_text('model = "shared"\n', encoding="utf-8")
        account = self.account_home()
        destination = account / "config.toml"
        os.link(source, destination)

        self.store._copy_shared_config(account, self.codex_home)

        self.assertNotEqual(destination.stat().st_ino, source.stat().st_ino)
        self.assertEqual(
            destination.read_text(encoding="utf-8"), source.read_text(encoding="utf-8")
        )

    def test_configuration_backups_do_not_overwrite_each_other(self):
        source = self.codex_home / "config.toml"
        source.write_text('model = "first"\n', encoding="utf-8")
        account = self.account_home()
        destination = account / "config.toml"
        destination.write_text('model = "original"\n', encoding="utf-8")

        with patch("super_agent.config.file_timestamp", return_value="fixed"):
            self.store._copy_shared_config(account, self.codex_home)
            source.write_text('model = "second"\n', encoding="utf-8")
            self.store._copy_shared_config(account, self.codex_home)

        backups = sorted(account.glob("config.toml.bak-*"))
        self.assertEqual(len(backups), 2)
        self.assertEqual(
            {backup.read_text(encoding="utf-8") for backup in backups},
            {'model = "original"\n', 'model = "first"\n'},
        )
        self.assertEqual(destination.read_text(encoding="utf-8"), 'model = "second"\n')

    def test_configuration_replacement_failure_preserves_the_current_file(self):
        (self.codex_home / "config.toml").write_text(
            'model = "new"\n', encoding="utf-8"
        )
        account = self.account_home()
        destination = account / "config.toml"
        destination.write_text('model = "old"\n', encoding="utf-8")

        with patch("super_agent.config.os.replace", side_effect=OSError("blocked")):
            with self.assertRaisesRegex(ConfigError, "Cannot share Codex configuration"):
                self.store._copy_shared_config(account, self.codex_home)

        self.assertEqual(destination.read_text(encoding="utf-8"), 'model = "old"\n')
        self.assertEqual(list(account.glob(".config.toml.*.tmp")), [])

    def test_configuration_refuses_a_symlinked_destination(self):
        (self.codex_home / "config.toml").write_text(
            'model = "new"\n', encoding="utf-8"
        )
        account = self.account_home()
        target = account / "local.toml"
        target.write_text('model = "old"\n', encoding="utf-8")
        (account / "config.toml").symlink_to(target)

        with self.assertRaisesRegex(ConfigError, "unsafe account configuration"):
            self.store._copy_shared_config(account, self.codex_home)

        self.assertEqual(target.read_text(encoding="utf-8"), 'model = "old"\n')

    def test_excluding_a_group_stops_it_following_the_store(self):
        self.store.set_session_groups(self.config, ["config"], include=False)
        (self.codex_home / "config.toml").write_text('model = "new"\n', encoding="utf-8")
        account = self.account_home()

        self.store.environment("codex", "2", self.config)

        self.assertFalse((account / "config.toml").exists())

    def test_prompt_history_becomes_one_shared_file(self):
        history = self.codex_home / "history.jsonl"
        history.write_text('{"prompt":"one"}\n', encoding="utf-8")
        account = self.account_home()

        self.store.environment("codex", "2", self.config)

        self.assertEqual(
            (account / "history.jsonl").stat().st_ino, history.stat().st_ino
        )

    def test_account_history_seeds_an_empty_shared_store(self):
        account = self.account_home()
        local = account / "history.jsonl"
        local.write_text('{"prompt":"account"}\n', encoding="utf-8")

        changed = self.store._link_shared_history(account, self.codex_home)

        shared = self.codex_home / "history.jsonl"
        self.assertEqual(changed, 1)
        self.assertEqual(shared.stat().st_ino, local.stat().st_ino)

    def test_a_rewritten_prompt_history_is_set_aside_not_discarded(self):
        history = self.codex_home / "history.jsonl"
        history.write_text('{"prompt":"shared"}\n', encoding="utf-8")
        account = self.account_home()
        (account / "history.jsonl").write_text('{"prompt":"local"}\n', encoding="utf-8")

        self.store.environment("codex", "2", self.config)

        backups = list(account.glob("history.jsonl.local-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            backups[0].read_text(encoding="utf-8"), '{"prompt":"local"}\n'
        )
        self.assertEqual(
            (account / "history.jsonl").stat().st_ino, history.stat().st_ino
        )

    def test_history_link_failure_preserves_the_account_history(self):
        history = self.codex_home / "history.jsonl"
        history.write_text('{"prompt":"shared"}\n', encoding="utf-8")
        account = self.account_home()
        local = account / "history.jsonl"
        local.write_text('{"prompt":"local"}\n', encoding="utf-8")

        with patch("super_agent.config.os.link", side_effect=OSError("cross-device")):
            with self.assertRaisesRegex(ConfigError, "same filesystem"):
                self.store._link_shared_history(account, self.codex_home)

        self.assertEqual(local.read_text(encoding="utf-8"), '{"prompt":"local"}\n')
        self.assertEqual(list(account.glob("history.jsonl.local-*")), [])

    def test_a_session_recorded_elsewhere_can_be_adopted_by_name(self):
        self.store.set_session_sharing(self.config, "isolated", "2")
        account = self.account_home()
        own = self.write_rollout(
            account, "2026/09/07", "rollout-2026-09-07T08-00-00-eeee.jsonl"
        )

        found = self.store.find_rollout(self.config, "eeee", skip=self.codex_home)
        self.assertEqual(found, own)
        adopted = self.store.adopt_rollout(self.codex_home, found)
        self.assertEqual(adopted.stat().st_ino, own.stat().st_ino)
        self.assertEqual(
            adopted,
            self.codex_home / "sessions" / "2026" / "09" / "07" / own.name,
        )

    def test_adopting_a_session_refuses_to_create_a_diverging_copy(self):
        self.store.set_session_sharing(self.config, "isolated", "2")
        account = self.account_home()
        own = self.write_rollout(
            account, "2026/09/07", "rollout-2026-09-07T08-00-00-eeee.jsonl"
        )

        with patch("super_agent.config.os.link", side_effect=OSError("cross-device")):
            with self.assertRaisesRegex(ConfigError, "same filesystem"):
                self.store.adopt_rollout(self.codex_home, own)

        destination = (
            self.codex_home / "sessions" / "2026" / "09" / "07" / own.name
        )
        self.assertFalse(destination.exists())

    def test_adopting_a_session_refuses_a_symlinked_transcript(self):
        account = self.account_home()
        target = self.write_rollout(
            account, "2026/09/07", "rollout-2026-09-07T08-00-00-target.jsonl"
        )
        linked = target.with_name("rollout-2026-09-07T08-00-00-linked.jsonl")
        linked.symlink_to(target)

        with self.assertRaisesRegex(ConfigError, "unsafe Codex transcript"):
            self.store.adopt_rollout(self.codex_home, linked)

    def test_sharing_keys_are_added_without_bumping_the_schema_version(self):
        legacy = default_config()
        legacy.pop("sessionSharing")
        self.store.save_raw = None
        path = self.store.config_path
        path.write_text(json.dumps(legacy), encoding="utf-8")

        config = self.store.load()

        self.assertEqual(config["version"], 2)
        self.assertEqual(config["sessionSharing"]["default"], "shared")
        self.assertIsNotNone(config["sessionSharing"]["since"])

    def test_an_existing_configuration_is_not_rewritten_on_every_load(self):
        first = self.store.config_path.stat().st_mtime_ns
        self.store.load()
        self.assertEqual(self.store.config_path.stat().st_mtime_ns, first)

    def test_unknown_sharing_groups_and_modes_are_refused(self):
        with self.assertRaisesRegex(ConfigError, "Unknown session sharing group"):
            self.store.set_session_groups(self.config, ["secrets"])
        with self.assertRaisesRegex(ConfigError, "Session sharing must be one of"):
            self.store.set_session_sharing(self.config, "sometimes")
