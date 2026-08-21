import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_sensitive_data import (
    ScanError,
    commit_sources,
    load_sensitive_hashes,
    scan_content,
    term_digest,
)


class SensitiveDataScanTests(unittest.TestCase):
    def rules(self, value, hashes=None, source="example.txt"):
        findings = scan_content(source, value.encode(), hashes or set())
        return {finding.rule for finding in findings}

    def test_detects_non_example_email(self):
        address = "person" + "@company.invalid"
        self.assertIn("email-address", self.rules(address))

    def test_allows_reserved_email_and_git_transport_examples(self):
        value = (
            "person@example.com git@github.com "
            "noreply@github.com noreply@anthropic.com secret-token@github.com"
        )
        self.assertNotIn("email-address", self.rules(value))

    def test_detects_absolute_user_home_path(self):
        value = "/" + "Users" + "/person/private.txt"
        self.assertIn("absolute-user-home-path", self.rules(value))

    def test_detects_hashed_sensitive_term_without_storing_plaintext(self):
        term = "confidentialbrand"
        digest = term_digest(term)
        self.assertEqual(digest, hashlib.sha256(term.encode()).hexdigest())
        findings = scan_content("notes.txt", f"About {term.title()}".encode(), {digest})
        self.assertIn("private-sensitive-term", {item.rule for item in findings})

    def test_redacts_path_when_sensitive_term_is_in_filename(self):
        term = "privateclient"
        findings = scan_content(f"docs/{term}.md", b"safe", {term_digest(term)})
        self.assertTrue(findings)
        self.assertTrue(all(item.source == "<redacted-sensitive-path>" for item in findings))

    def test_comment_cannot_bypass_email_scan(self):
        value = "person" + "@company.invalid  # privacy-scan: allow"
        self.assertIn("email-address", self.rules(value))

    def test_loads_digest_from_environment_for_ci(self):
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SUPER_CODEX_SENSITIVE_TERM_DIGESTS": digest}
        ):
            self.assertEqual(load_sensitive_hashes(Path(directory)), {digest})

    def test_rejects_invalid_environment_digest(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SUPER_CODEX_SENSITIVE_TERM_DIGESTS": "invalid"}
        ):
            with self.assertRaises(ScanError):
                load_sensitive_hashes(Path(directory))

    @patch("scripts.check_sensitive_data.run_git")
    def test_commit_metadata_is_included_as_scan_content(self, run_git):
        commit_id = "a" * 40
        metadata = b"author Person <person" + b"@company.invalid> 1 +0000\n\nmessage\n"
        run_git.side_effect = [commit_id + "\n", metadata]
        source, content = next(commit_sources(["--all"]))
        self.assertEqual(source, f"<commit-metadata>@{commit_id[:12]}")
        self.assertIn("email-address", self.rules(content.decode(), source=source))

    def test_pre_push_empty_input_is_safe(self):
        from scripts.check_sensitive_data import main

        self.assertEqual(
            main(["--pre-push", "--allow-empty-terms"], input_stream=io.StringIO("")),
            0,
        )


if __name__ == "__main__":
    unittest.main()
