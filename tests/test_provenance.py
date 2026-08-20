import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from super_agent.cli import main
from super_agent.provenance import installation_provenance


class ProvenanceTests(unittest.TestCase):
    def distribution(self, version="1.2.3", direct_url=None):
        distribution = Mock()
        distribution.version = version
        distribution.read_text.return_value = (
            json.dumps(direct_url) if direct_url is not None else None
        )
        return distribution

    @patch("super_agent.provenance.metadata.distribution")
    def test_reports_vcs_revision_and_commit(self, find_distribution):
        find_distribution.return_value = self.distribution(
            direct_url={
                "url": "git+ssh://git@github.com/driwand/super-codex.git",
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": "v1.2.3",
                    "commit_id": "abc123",
                },
            }
        )

        result = installation_provenance()

        self.assertEqual(result["version"], "1.2.3")
        self.assertEqual(result["installType"], "vcs")
        self.assertEqual(result["requestedRevision"], "v1.2.3")
        self.assertEqual(result["commit"], "abc123")
        self.assertEqual(
            result["source"],
            "git+ssh://git@github.com/driwand/super-codex.git",
        )

    @patch("super_agent.provenance.metadata.distribution")
    def test_redacts_credentials_from_direct_url(self, find_distribution):
        find_distribution.return_value = self.distribution(
            direct_url={
                "url": "https://secret-token@github.com/driwand/super-codex.git",
                "vcs_info": {"vcs": "git", "commit_id": "abc123"},
            }
        )

        result = installation_provenance()

        self.assertEqual(
            result["source"], "https://github.com/driwand/super-codex.git"
        )
        self.assertNotIn("secret-token", json.dumps(result))

    @patch("super_agent.provenance.metadata.distribution")
    def test_missing_direct_metadata_is_reported_as_index_install(self, find_distribution):
        find_distribution.return_value = self.distribution(direct_url=None)

        result = installation_provenance()

        self.assertEqual(result["installType"], "index")
        self.assertIsNone(result["source"])

    @patch("super_agent.provenance.metadata.distribution")
    @patch("super_agent.provenance.standalone_metadata")
    def test_standalone_metadata_takes_precedence(self, build_metadata, distribution):
        build_metadata.return_value = {
            "installType": "standalone-release",
            "version": "1.2.3",
            "tag": "v1.2.3",
            "commit": "abc123",
            "source": "https://github.com/driwand/super-codex/releases/tag/v1.2.3",
        }

        result = installation_provenance()

        self.assertEqual(result["installType"], "standalone-release")
        self.assertEqual(result["requestedRevision"], "v1.2.3")
        self.assertEqual(result["commit"], "abc123")
        distribution.assert_not_called()

    @patch("super_agent.cli.print_installation_provenance")
    @patch("super_agent.cli.Store.load", side_effect=AssertionError("config was loaded"))
    def test_version_command_does_not_load_configuration(self, load, print_version):
        self.assertEqual(main(["version", "--json"]), 0)
        print_version.assert_called_once_with(True)
        load.assert_not_called()

    @patch("super_agent.provenance.metadata.distribution")
    def test_version_json_has_a_stable_schema(self, find_distribution):
        find_distribution.return_value = self.distribution()
        output = io.StringIO()

        with redirect_stdout(output):
            code = main(["version", "--json"])

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["package"], "super-codex")
        self.assertIn("executable", result)


if __name__ == "__main__":
    unittest.main()
