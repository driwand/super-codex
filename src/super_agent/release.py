"""Standalone GitHub Release installation lifecycle."""

import hashlib
import json
import os
import pkgutil
import re
import shutil
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


class ReleaseError(Exception):
    """A safe standalone release operation could not be completed."""


def standalone_metadata():
    try:
        raw = pkgutil.get_data("super_agent", "_build.json")
    except OSError:
        return None
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if isinstance(value, dict) and value.get("installType") == "standalone-release":
        return value
    return None


def _tag_version(tag):
    match = TAG_PATTERN.fullmatch(tag or "")
    if not match:
        raise ReleaseError("release versions must use the form vX.Y.Z")
    return tuple(int(value) for value in match.groups())


def _manifest_url(tag=None):
    if tag is None:
        return f"https://github.com/{REPOSITORY}/releases/latest/download/release.json"
    _tag_version(tag)
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/release.json"


def _asset_url(tag):
    _tag_version(tag)
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ASSET_NAME}"


def _read_url(url, limit):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            value = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseError(f"download failed: {exc}") from exc
    if len(value) > limit:
        raise ReleaseError("download exceeded its safety limit")
    return value


def validate_manifest(value):
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ReleaseError("release manifest has an unsupported schema")
    tag = value.get("tag")
    version = value.get("version")
    _tag_version(tag)
    if version != tag[1:]:
        raise ReleaseError("release manifest version and tag disagree")
    if value.get("asset") != ASSET_NAME:
        raise ReleaseError("release manifest names an unexpected asset")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ReleaseError("release manifest has an invalid checksum")
    size = value.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ASSET_BYTES:
        raise ReleaseError("release manifest has an invalid asset size")
    commit = value.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("release manifest has an invalid commit")
    return value


def fetch_manifest(tag=None):
    raw = _read_url(_manifest_url(tag), MAX_MANIFEST_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReleaseError("release manifest is not valid JSON") from exc
    manifest = validate_manifest(value)
    if tag is not None and manifest["tag"] != tag:
        raise ReleaseError("downloaded release does not match the requested tag")
    return manifest


def archive_metadata(path):
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("super_agent/_build.json")
        value = json.loads(raw.decode("utf-8"))
    except (KeyError, OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
        raise ReleaseError("downloaded asset is not a Super Codex release") from exc
    if not isinstance(value, dict) or value.get("installType") != "standalone-release":
        raise ReleaseError("downloaded asset has invalid build metadata")
    return value


def _validate_executable(path):
    try:
        details = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"cannot inspect installed executable: {exc}") from exc
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise ReleaseError("installed executable is not a regular file")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ReleaseError("installed executable is owned by another user")


def _installed_executable():
    invoked = Path(sys.argv[0]).expanduser()
    if "/" not in sys.argv[0]:
        located = shutil.which(invoked.name)
        if located is None:
            raise ReleaseError("cannot locate the installed executable on PATH")
        invoked = Path(located)
    return invoked.resolve()


def _download_asset(manifest, directory):
    descriptor, temporary_name = tempfile.mkstemp(prefix=".sc-update-", dir=str(directory))
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    received = 0
    try:
        try:
            with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(
                _asset_url(manifest["tag"]), timeout=30
            ) as response:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > manifest["size"] or received > MAX_ASSET_BYTES:
                        raise ReleaseError("downloaded asset exceeded its declared size")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except (OSError, urllib.error.URLError) as exc:
            raise ReleaseError(f"asset download failed: {exc}") from exc
        if received != manifest["size"]:
            raise ReleaseError("downloaded asset size does not match the manifest")
        if digest.hexdigest() != manifest["sha256"]:
            raise ReleaseError("downloaded asset checksum does not match the manifest")
        embedded = archive_metadata(temporary)
        for key in ("version", "tag", "commit"):
            if embedded.get(key) != manifest.get(key):
                raise ReleaseError(f"downloaded asset {key} does not match the manifest")
        os.chmod(temporary, 0o755)
        return temporary
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def run_update(check=False, tag=None):
    current = standalone_metadata()
    if current is None:
        raise ReleaseError("updates are available only for standalone GitHub Release installs")
    manifest = fetch_manifest(tag)
    current_tag = current.get("tag")
    if manifest["tag"] == current_tag or (
        tag is None and _tag_version(manifest["tag"]) <= _tag_version(current_tag)
    ):
        print(f"Super Codex {current.get('version')} is already up to date.")
        return 0
    if check:
        print(f"Update available: {current_tag} -> {manifest['tag']}")
        return 0

    executable = _installed_executable()
    _validate_executable(executable)
    temporary = _download_asset(manifest, executable.parent)
    try:
        os.replace(temporary, executable)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ReleaseError(f"could not replace installed executable: {exc}") from exc
    print(f"Updated Super Codex to {manifest['tag']}.")
    return 0


def run_uninstall():
    if standalone_metadata() is None:
        raise ReleaseError("uninstall is available only for standalone GitHub Release installs")
    executable = _installed_executable()
    _validate_executable(executable)
    alias = executable.parent / "super-codex"
    if alias.is_symlink() and alias.resolve() == executable:
        alias.unlink()
    executable.unlink()
    print("Removed standalone Super Codex commands.")
    print("Kept configuration, profiles, sessions, and provider state unchanged.")
    return 0
