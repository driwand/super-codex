import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_version.py"
sys.path.insert(0, str(ROOT / "src"))

from super_agent import __version__


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

    def test_matching_release_tag_is_accepted(self):
        result = self.run_check(f"v{__version__}")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mismatched_release_tag_is_rejected(self):
        result = self.run_check("v999.0.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match package version", result.stderr)


if __name__ == "__main__":
    unittest.main()
