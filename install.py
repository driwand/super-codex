#!/usr/bin/env python3
"""Install the standalone Super Codex GitHub Release without a package manager."""

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


REPOSITORY = "driwand/super-codex"
ASSET_NAME = "sc"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ASSET_BYTES = 16 * 1024 * 1024
TAG_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class InstallError(Exception):
    pass


def manifest_url(tag=None):
    if tag is None:
        return f"https://github.com/{REPOSITORY}/releases/latest/download/release.json"
    if not TAG_PATTERN.fullmatch(tag):
        raise InstallError("release versions must use the form vX.Y.Z")
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/release.json"


def asset_url(tag):
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ASSET_NAME}"


def read_url(url, limit):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            value = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise InstallError(f"download failed: {exc}") from exc
    if len(value) > limit:
        raise InstallError("download exceeded its safety limit")
    return value


def validate_manifest(value, requested_tag=None):
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise InstallError("release manifest has an unsupported schema")
    tag = value.get("tag")
    version = value.get("version")
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        raise InstallError("release manifest has an invalid tag")
    if version != tag[1:]:
        raise InstallError("release manifest version and tag disagree")
    if requested_tag is not None and tag != requested_tag:
        raise InstallError("downloaded release does not match the requested tag")
    if value.get("asset") != ASSET_NAME:
        raise InstallError("release manifest names an unexpected asset")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise InstallError("release manifest has an invalid checksum")
    size = value.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ASSET_BYTES:
        raise InstallError("release manifest has an invalid asset size")
    commit = value.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise InstallError("release manifest has an invalid commit")
    return value


def fetch_manifest(tag=None):
    raw = read_url(manifest_url(tag), MAX_MANIFEST_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError("release manifest is not valid JSON") from exc
    return validate_manifest(value, tag)


def archive_metadata(path):
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("super_agent/_build.json")
        value = json.loads(raw.decode("utf-8"))
    except (KeyError, OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
        raise InstallError("asset is not a standalone Super Codex release") from exc
    if not isinstance(value, dict) or value.get("installType") != "standalone-release":
        raise InstallError("asset has invalid build metadata")
    return value


def preflight(target, alias):
    if os.path.lexists(target):
        details = target.lstat()
        if target.is_symlink() or not stat.S_ISREG(details.st_mode):
            raise InstallError(f"refusing to replace non-regular path: {target}")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise InstallError(f"refusing to replace a file owned by another user: {target}")
        archive_metadata(target)
    if os.path.lexists(alias):
        if not alias.is_symlink() or alias.resolve() != target.resolve():
            raise InstallError(f"refusing to replace unrelated command: {alias}")


def download_asset(manifest, directory):
    descriptor, temporary_name = tempfile.mkstemp(prefix=".sc-install-", dir=str(directory))
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    received = 0
    try:
        try:
            with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(
                asset_url(manifest["tag"]), timeout=30
            ) as response:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > manifest["size"] or received > MAX_ASSET_BYTES:
                        raise InstallError("asset exceeded its declared size")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except (OSError, urllib.error.URLError) as exc:
            raise InstallError(f"asset download failed: {exc}") from exc
        if received != manifest["size"]:
            raise InstallError("asset size does not match the release manifest")
        if digest.hexdigest() != manifest["sha256"]:
            raise InstallError("asset checksum does not match the release manifest")
        embedded = archive_metadata(temporary)
        for key in ("version", "tag", "commit"):
            if embedded.get(key) != manifest.get(key):
                raise InstallError(f"asset {key} does not match the release manifest")
        os.chmod(temporary, 0o755)
        return temporary
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def install(tag=None, bin_directory=None):
    if platform.system() not in ("Darwin", "Linux"):
        raise InstallError("only macOS and Linux are supported")
    if sys.version_info < (3, 9):
        raise InstallError("Python 3.9 or newer is required")
    directory = Path(bin_directory or "~/.local/bin").expanduser().absolute()
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = directory / "sc"
    alias = directory / "super-codex"
    preflight(target, alias)
    manifest = fetch_manifest(tag)
    temporary = download_asset(manifest, directory)
    try:
        os.replace(temporary, target)
        alias_temporary = directory / f".super-codex-{os.getpid()}"
        if os.path.lexists(alias_temporary):
            alias_temporary.unlink()
        alias_temporary.symlink_to("sc")
        os.replace(alias_temporary, alias)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise InstallError(f"installation failed: {exc}") from exc
    print(f"Installed Super Codex {manifest['tag']} at {target}")
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if str(directory) not in path_entries:
        print(f"Add this directory to PATH: {directory}")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", metavar="vX.Y.Z", help="Install an exact release")
    parser.add_argument("--bin-dir", help="Installation directory (default: ~/.local/bin)")
    args = parser.parse_args(argv)
    try:
        install(args.version, args.bin_dir)
    except InstallError as exc:
        print(f"install: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
