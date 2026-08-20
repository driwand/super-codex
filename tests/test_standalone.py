import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import install as installer
from scripts.build_standalone import build
from super_agent import __version__
from super_agent.cli import main as cli_main
from super_agent.release import ReleaseError, run_uninstall, run_update, validate_manifest


COMMIT = "0123456789abcdef0123456789abcdef01234567"


def write_asset(path, tag, commit=COMMIT):
    metadata = {
        "schemaVersion": 1,
        "installType": "standalone-release",
        "version": tag[1:],
        "tag": tag,
        "commit": commit,
        "source": f"https://github.com/driwand/super-codex/releases/tag/{tag}",
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("super_agent/_build.json", json.dumps(metadata))
        archive.writestr("__main__.py", "")
    content = path.read_bytes()
    return {
        "schemaVersion": 1,
        "version": tag[1:],
        "tag": tag,
        "commit": commit,
        "asset": "sc",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


class StandaloneBuildTests(unittest.TestCase):
    def test_build_runs_through_both_entry_point_names(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            output = Path(temporary_name)
            executable, manifest = build(f"v{__version__}", COMMIT, output)
            alias = output / "super-codex"
            alias.symlink_to("sc")

            short = subprocess.run(
                [str(executable), "version", "--json"],
                text=True,
                capture_output=True,
                check=False,
            )
            long = subprocess.run(
                [str(alias), "--version"], text=True, capture_output=True, check=False
            )

            self.assertEqual(short.returncode, 0, short.stderr)
            provenance = json.loads(short.stdout)
            self.assertEqual(provenance["installType"], "standalone-release")
            self.assertEqual(provenance["commit"], COMMIT)
            self.assertEqual(long.returncode, 0, long.stderr)
            self.assertEqual(long.stdout.strip(), f"sc {__version__}")
            self.assertEqual(manifest["sha256"], hashlib.sha256(executable.read_bytes()).hexdigest())

    def test_build_rejects_a_mismatched_tag(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            with self.assertRaisesRegex(ValueError, "tag must match"):
                build("v999.0.0", COMMIT, Path(temporary_name))


class StandaloneInstallerTests(unittest.TestCase):
    def test_installs_verified_asset_and_alias_without_package_manager(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            asset = root / "asset"
            manifest = write_asset(asset, "v1.2.3")
            destination = root / "bin"
            with patch.object(installer, "fetch_manifest", return_value=manifest), patch.object(
                installer, "asset_url", return_value=asset.as_uri()
            ), patch.object(installer.platform, "system", return_value="Linux"):
                installed = installer.install(bin_directory=str(destination))

            self.assertEqual(installed.read_bytes(), asset.read_bytes())
            self.assertTrue(os.access(installed, os.X_OK))
            self.assertTrue((destination / "super-codex").is_symlink())
            self.assertEqual((destination / "super-codex").resolve(), installed.resolve())

    def test_refuses_to_replace_an_unrelated_command(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            destination = Path(temporary_name)
            (destination / "sc").write_text("unrelated", encoding="utf-8")

            with self.assertRaisesRegex(installer.InstallError, "not a standalone"):
                installer.install(bin_directory=str(destination))

    def test_checksum_failure_leaves_no_installable_asset(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            asset = root / "asset"
            manifest = write_asset(asset, "v1.2.3")
            manifest["sha256"] = "0" * 64
            destination = root / "bin"
            destination.mkdir()
            with patch.object(installer, "asset_url", return_value=asset.as_uri()):
                with self.assertRaisesRegex(installer.InstallError, "checksum"):
                    installer.download_asset(manifest, destination)
            self.assertEqual(list(destination.iterdir()), [])

    def test_interrupted_download_removes_temporary_asset(self):
        class InterruptedResponse:
            calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def read(self, unused_size):
                self.calls += 1
                if self.calls == 1:
                    return b"partial"
                raise OSError("connection interrupted")

        with tempfile.TemporaryDirectory() as temporary_name:
            destination = Path(temporary_name)
            manifest = {
                "tag": "v1.2.3",
                "size": 100,
                "sha256": "0" * 64,
            }
            with patch.object(
                installer.urllib.request, "urlopen", return_value=InterruptedResponse()
            ):
                with self.assertRaisesRegex(installer.InstallError, "interrupted"):
                    installer.download_asset(manifest, destination)
            self.assertEqual(list(destination.iterdir()), [])

    def test_installer_does_not_invoke_external_package_managers(self):
        source = (ROOT / "install.py").read_text(encoding="utf-8")
        for command in ("uv ", "pipx", "pip install", "sudo"):
            self.assertNotIn(command, source)


class StandaloneUpdateTests(unittest.TestCase):
    def test_update_atomically_replaces_the_executable(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            target = root / "sc"
            target.write_text("old", encoding="utf-8")
            target.chmod(0o755)
            asset = root / "asset"
            manifest = write_asset(asset, "v1.3.0")
            current = {"installType": "standalone-release", "tag": "v1.2.3", "version": "1.2.3"}
            output = io.StringIO()
            with patch("super_agent.release.standalone_metadata", return_value=current), patch(
                "super_agent.release.fetch_manifest", return_value=manifest
            ), patch("super_agent.release._asset_url", return_value=asset.as_uri()), patch.object(
                sys, "argv", [str(target)]
            ), redirect_stdout(output):
                self.assertEqual(run_update(), 0)

            self.assertEqual(target.read_bytes(), asset.read_bytes())
            self.assertIn("v1.3.0", output.getvalue())

    def test_update_check_does_not_replace_the_executable(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            current = {
                "installType": "standalone-release",
                "tag": "v1.2.3",
                "version": "1.2.3",
            }
            manifest = write_asset(Path(temporary_name) / "asset", "v1.3.0")
            output = io.StringIO()
            with patch(
                "super_agent.release.standalone_metadata", return_value=current
            ), patch(
                "super_agent.release.fetch_manifest", return_value=manifest
            ), redirect_stdout(output):
                self.assertEqual(run_update(check=True), 0)
            self.assertIn("v1.2.3 -> v1.3.0", output.getvalue())

    def test_exact_older_tag_is_allowed_for_rollback(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            target = root / "sc"
            target.write_text("old", encoding="utf-8")
            target.chmod(0o755)
            replacement = root / "replacement"
            replacement.write_text("rolled back", encoding="utf-8")
            current = {
                "installType": "standalone-release",
                "tag": "v1.3.0",
                "version": "1.3.0",
            }
            manifest = {
                "tag": "v1.2.3",
                "version": "1.2.3",
                "commit": COMMIT,
            }
            with patch(
                "super_agent.release.standalone_metadata", return_value=current
            ), patch(
                "super_agent.release.fetch_manifest", return_value=manifest
            ) as fetch, patch(
                "super_agent.release._download_asset", return_value=replacement
            ), patch.object(sys, "argv", [str(target)]):
                self.assertEqual(run_update(tag="v1.2.3"), 0)

            fetch.assert_called_once_with("v1.2.3")
            self.assertEqual(target.read_text(encoding="utf-8"), "rolled back")

    def test_uninstall_removes_commands_but_not_state(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            target = root / "sc"
            target.write_text("standalone", encoding="utf-8")
            target.chmod(0o755)
            alias = root / "super-codex"
            alias.symlink_to("sc")
            state = root / "config.json"
            state.write_text("keep", encoding="utf-8")
            current = {"installType": "standalone-release", "tag": "v1.2.3"}
            with patch("super_agent.release.standalone_metadata", return_value=current), patch.object(
                sys, "argv", [str(target)]
            ):
                self.assertEqual(run_uninstall(), 0)

            self.assertFalse(target.exists())
            self.assertFalse(alias.exists())
            self.assertEqual(state.read_text(encoding="utf-8"), "keep")

    def test_source_checkout_cannot_self_update_or_uninstall(self):
        with patch("super_agent.release.standalone_metadata", return_value=None):
            with self.assertRaisesRegex(ReleaseError, "standalone"):
                run_update()
            with self.assertRaisesRegex(ReleaseError, "standalone"):
                run_uninstall()

    def test_update_and_uninstall_commands_do_not_load_configuration(self):
        with patch("super_agent.cli.run_update", return_value=0) as update, patch(
            "super_agent.cli.run_uninstall", return_value=0
        ) as uninstall, patch(
            "super_agent.cli.Store.load", side_effect=AssertionError("config was loaded")
        ) as load:
            self.assertEqual(cli_main(["update", "--check"]), 0)
            self.assertEqual(cli_main(["uninstall"]), 0)

        update.assert_called_once_with(True, None)
        uninstall.assert_called_once_with()
        load.assert_not_called()

    def test_manifest_rejects_malformed_release_data(self):
        valid = {
            "schemaVersion": 1,
            "version": "1.2.3",
            "tag": "v1.2.3",
            "commit": COMMIT,
            "asset": "sc",
            "size": 100,
            "sha256": "0" * 64,
        }
        for key, value in (
            ("tag", "main"),
            ("commit", "short"),
            ("asset", "other"),
            ("size", 16 * 1024 * 1024 + 1),
        ):
            malformed = dict(valid)
            malformed[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ReleaseError):
                validate_manifest(malformed)

    def test_uninstall_resolves_a_command_launched_through_path(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            target = Path(temporary_name) / "sc"
            target.write_text("standalone", encoding="utf-8")
            target.chmod(0o755)
            current = {"installType": "standalone-release", "tag": "v1.2.3"}
            with patch(
                "super_agent.release.standalone_metadata", return_value=current
            ), patch("super_agent.release.shutil.which", return_value=str(target)), patch.object(
                sys, "argv", ["sc"]
            ):
                self.assertEqual(run_uninstall(), 0)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
