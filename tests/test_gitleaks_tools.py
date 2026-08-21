import hashlib
import io
import tarfile
import unittest

from scripts.install_gitleaks import InstallError, archive_binary, asset_for
from scripts.run_gitleaks import GitleaksError, pre_push_log_options


class GitleaksInstallerTests(unittest.TestCase):
    def archive(self, content=b"binary"):
        value = io.BytesIO()
        with tarfile.open(fileobj=value, mode="w:gz") as archive:
            details = tarfile.TarInfo("gitleaks")
            details.mode = 0o755
            details.size = len(content)
            archive.addfile(details, io.BytesIO(content))
        return value.getvalue()

    def test_selects_supported_assets(self):
        self.assertEqual(asset_for("Darwin", "arm64")[0], "gitleaks_8.29.1_darwin_arm64.tar.gz")
        self.assertEqual(asset_for("Linux", "x86_64")[0], "gitleaks_8.29.1_linux_x64.tar.gz")

    def test_rejects_unsupported_platform(self):
        with self.assertRaises(InstallError):
            asset_for("Windows", "x86_64")

    def test_extracts_only_checksum_verified_binary(self):
        archive = self.archive(b"verified")
        digest = hashlib.sha256(archive).hexdigest()
        self.assertEqual(archive_binary(archive, digest), b"verified")

    def test_rejects_checksum_mismatch(self):
        with self.assertRaises(InstallError):
            archive_binary(self.archive(), "0" * 64)


class GitleaksRunnerTests(unittest.TestCase):
    def test_builds_exact_pre_push_revision_range(self):
        local = "a" * 40
        remote = "b" * 40
        value = f"refs/heads/main {local} refs/heads/main {remote}\n"
        self.assertEqual(
            pre_push_log_options(io.StringIO(value)),
            f"{local} --not {remote}",
        )

    def test_skips_deleted_ref(self):
        value = f"(delete) {'0' * 40} refs/heads/old {'b' * 40}\n"
        self.assertIsNone(pre_push_log_options(io.StringIO(value)))

    def test_rejects_invalid_pre_push_input(self):
        with self.assertRaises(GitleaksError):
            pre_push_log_options(io.StringIO("unsafe\n"))


if __name__ == "__main__":
    unittest.main()
