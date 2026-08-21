#!/usr/bin/env python3
"""Install a checksum-pinned Gitleaks binary for local hooks and CI."""

import argparse
import hashlib
import io
import os
import platform
import stat
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


# Keep this on a release that passes scripts/run_gitleaks.py's canary. Version
# 8.30.1 failed that self-test; never update only because a newer tag exists.
VERSION = "8.29.1"
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_BINARY_BYTES = 50 * 1024 * 1024
ASSETS = {
    ("Darwin", "x86_64"): (
        "gitleaks_8.29.1_darwin_x64.tar.gz",
        "2cd739c684bf3f543f4f37774075c276e40a72bb16c4c5bb9dfd27bf4a4465a7",
    ),
    ("Darwin", "arm64"): (
        "gitleaks_8.29.1_darwin_arm64.tar.gz",
        "69836c841d7e648fb30ff4846f8c3587855c5754ed02b8510caaf6008f65d177",
    ),
    ("Linux", "x86_64"): (
        "gitleaks_8.29.1_linux_x64.tar.gz",
        "e4eb209d04e20339d77122a3bdf9cd41351255cfb27ebcb75e85325e04f88924",
    ),
    ("Linux", "aarch64"): (
        "gitleaks_8.29.1_linux_arm64.tar.gz",
        "691f826ce7c1c564c9c02d0f9025e8e70803e3816707a4be6224408a06a81eaa",
    ),
    ("Linux", "arm64"): (
        "gitleaks_8.29.1_linux_arm64.tar.gz",
        "691f826ce7c1c564c9c02d0f9025e8e70803e3816707a4be6224408a06a81eaa",
    ),
}


class InstallError(RuntimeError):
    pass


def asset_for(system=None, machine=None):
    key = (system or platform.system(), machine or platform.machine())
    try:
        return ASSETS[key]
    except KeyError as exc:
        raise InstallError(f"unsupported Gitleaks platform: {key[0]} {key[1]}") from exc


def download(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            value = response.read(MAX_ARCHIVE_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise InstallError(f"Gitleaks download failed: {exc}") from exc
    if len(value) > MAX_ARCHIVE_BYTES:
        raise InstallError("Gitleaks archive exceeded its safety limit")
    return value


def archive_binary(value, expected_digest):
    if hashlib.sha256(value).hexdigest() != expected_digest:
        raise InstallError("Gitleaks archive checksum mismatch")
    try:
        with tarfile.open(fileobj=io.BytesIO(value), mode="r:gz") as archive:
            members = [item for item in archive.getmembers() if item.name in ("gitleaks", "./gitleaks")]
            if len(members) != 1 or not members[0].isfile():
                raise InstallError("Gitleaks archive has no unique regular executable")
            if not 0 < members[0].size <= MAX_BINARY_BYTES:
                raise InstallError("Gitleaks executable has an invalid size")
            extracted = archive.extractfile(members[0])
            if extracted is None:
                raise InstallError("Gitleaks executable could not be read")
            binary = extracted.read(MAX_BINARY_BYTES + 1)
    except (OSError, tarfile.TarError) as exc:
        raise InstallError(f"invalid Gitleaks archive: {exc}") from exc
    if not binary or len(binary) > MAX_BINARY_BYTES:
        raise InstallError("Gitleaks executable exceeded its safety limit")
    return binary


def secure_directory(path):
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        details = path.lstat()
    except OSError as exc:
        raise InstallError(f"cannot create Gitleaks tool directory: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise InstallError("refusing unsafe Gitleaks tool directory")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise InstallError("Gitleaks tool directory is owned by another user")
    os.chmod(path, 0o700)


def install(destination):
    asset, digest = asset_for()
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/{asset}"
    binary = archive_binary(download(url), digest)
    destination = Path(destination).absolute()
    secure_directory(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".gitleaks-", dir=str(destination.parent))
    try:
        os.fchmod(descriptor, 0o700)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(binary)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    print(f"Installed checksum-verified Gitleaks {VERSION} at {destination}")
    return destination


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--destination", type=Path, default=root / ".tools" / "gitleaks")
    args = value.parse_args(argv)
    try:
        install(args.destination)
    except InstallError as exc:
        value.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
