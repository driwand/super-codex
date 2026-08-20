import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.log"
        self.config = self.root / "config" / "super-codex" / "config.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{"keep": true}\n', encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def add_entry_points(self):
        self.executable("sc", 'printf "sc 0.3.2\\n"\n')
        self.executable("super-codex", 'printf "sc 0.3.2\\n"\n')

    def environment(self):
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": str(self.bin) + os.pathsep + environment["PATH"],
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "TEST_COMMAND_LOG": str(self.log),
                "TEST_UV_ROOT": str(self.root / "uv-tools"),
                "TEST_PIPX_ROOT": str(self.root / "pipx"),
            }
        )
        return environment

    def run_installer(self, *arguments):
        return subprocess.run(
            ["sh", str(INSTALLER), *arguments],
            cwd=ROOT,
            env=self.environment(),
            text=True,
            capture_output=True,
            check=False,
        )

    def fake_uv(self):
        self.executable(
            "uv",
            'if [ "$1 $2" = "tool dir" ]; then\n'
            '  printf "%s\\n" "$TEST_UV_ROOT"\n'
            "  exit 0\n"
            "fi\n"
            'printf "uv %s\\n" "$*" >> "$TEST_COMMAND_LOG"\n',
        )

    def fake_pipx(self):
        self.executable(
            "pipx",
            'if [ "$1 $2 ${3:-}" = "environment --value PIPX_HOME" ]; then\n'
            '  printf "%s\\n" "$TEST_PIPX_ROOT"\n'
            "  exit 0\n"
            "fi\n"
            'printf "pipx %s\\n" "$*" >> "$TEST_COMMAND_LOG"\n',
        )

    def test_install_prefers_uv_and_preserves_configuration(self):
        self.fake_uv()
        self.fake_pipx()
        self.add_entry_points()

        result = self.run_installer("install", "--source", str(ROOT))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8"), f"uv tool install {ROOT}\n"
        )
        self.assertEqual(self.config.read_text(encoding="utf-8"), '{"keep": true}\n')

    def test_install_reuses_existing_pipx_owner(self):
        self.fake_uv()
        self.fake_pipx()
        self.add_entry_points()
        (self.root / "pipx" / "venvs" / "super-codex").mkdir(parents=True)

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8"), "pipx reinstall super-codex\n"
        )

    def test_update_uses_recorded_uv_install(self):
        self.fake_uv()
        self.add_entry_points()
        (self.root / "uv-tools" / "super-codex").mkdir(parents=True)

        result = self.run_installer("update")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8"),
            "uv tool upgrade --reinstall super-codex\n",
        )

    def test_install_can_switch_existing_pipx_install_to_a_new_tag(self):
        self.fake_pipx()
        self.add_entry_points()
        (self.root / "pipx" / "venvs" / "super-codex").mkdir(parents=True)
        source = "git+ssh://git@github.com/driwand/super-codex.git@v0.4.0"

        result = self.run_installer("install", "--source", source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8"), f"pipx install --force {source}\n"
        )

    def test_refuses_ambiguous_dual_ownership(self):
        self.fake_uv()
        self.fake_pipx()
        (self.root / "uv-tools" / "super-codex").mkdir(parents=True)
        (self.root / "pipx" / "venvs" / "super-codex").mkdir(parents=True)

        result = self.run_installer("update")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed by both uv and pipx", result.stderr)
        self.assertFalse(self.log.exists())

    def test_uninstall_keeps_configuration(self):
        self.fake_pipx()
        (self.root / "pipx" / "venvs" / "super-codex").mkdir(parents=True)

        result = self.run_installer("uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8"), "pipx uninstall super-codex\n"
        )
        self.assertEqual(self.config.read_text(encoding="utf-8"), '{"keep": true}\n')
        self.assertIn("Kept user configuration", result.stdout)

    def test_installer_contains_no_privilege_or_permission_bypass(self):
        source = INSTALLER.read_text(encoding="utf-8")
        forbidden = ("sudo", "dangerously-bypass", "skip-permissions")
        for value in forbidden:
            self.assertNotIn(value, source)


if __name__ == "__main__":
    unittest.main()
