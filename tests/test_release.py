import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_version.py"
sys.path.insert(0, str(ROOT / "src"))

from super_agent import __version__
from scripts.check_release_version import unreleased_has_entries


class ReleaseTests(unittest.TestCase):
    def run_check(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_versions_and_changelog_are_consistent(self):
        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"v{__version__}", result.stdout)

    def test_unreleased_entries_are_detected(self):
        changelog = """# Changelog

## [Unreleased]

- Pending release note.

## [1.0.0]
"""

        self.assertTrue(unreleased_has_entries(changelog))

    def test_mismatched_release_tag_is_rejected(self):
        result = self.run_check("v999.0.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match package version", result.stderr)


if __name__ == "__main__":
    unittest.main()
