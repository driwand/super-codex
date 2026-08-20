"""Report how the running Super Codex package was installed."""

import json
import shutil
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .release import standalone_metadata


def _safe_url(value):
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme and "@" in value:
        userinfo, host_path = value.rsplit("@", 1)
        return ("git@" if userinfo == "git" else "") + host_path
    if "@" not in parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))
    host = parsed.netloc.rsplit("@", 1)[1]
    userinfo = parsed.netloc.rsplit("@", 1)[0]
    safe_userinfo = "git@" if userinfo == "git" else ""
    return urlunsplit(
        (parsed.scheme, safe_userinfo + host, parsed.path, "", parsed.fragment)
    )


def _executable_path():
    invoked = Path(sys.argv[0]).expanduser()
    if "/" in sys.argv[0]:
        return str(invoked.absolute())
    located = shutil.which(invoked.name)
    return str(Path(located).absolute()) if located else str(invoked.absolute())


def installation_provenance():
    result = {
        "schemaVersion": 1,
        "package": "super-codex",
        "version": __version__,
        "executable": _executable_path(),
        "installType": "source-checkout",
        "source": None,
        "vcs": None,
        "requestedRevision": None,
        "commit": None,
    }
    standalone = standalone_metadata()
    if standalone is not None:
        result.update(
            {
                "version": standalone.get("version", __version__),
                "installType": "standalone-release",
                "source": standalone.get("source"),
                "vcs": "git",
                "requestedRevision": standalone.get("tag"),
                "commit": standalone.get("commit"),
            }
        )
        return result
    try:
        distribution = metadata.distribution("super-codex")
    except metadata.PackageNotFoundError:
        return result

    result["version"] = distribution.version
    result["installType"] = "index"
    raw = distribution.read_text("direct_url.json")
    if not raw:
        return result
    try:
        direct = json.loads(raw)
    except (TypeError, ValueError):
        return result
    if not isinstance(direct, dict):
        return result

    result["source"] = _safe_url(direct.get("url"))
    vcs = direct.get("vcs_info")
    directory = direct.get("dir_info")
    archive = direct.get("archive_info")
    if isinstance(vcs, dict):
        result["installType"] = "vcs"
        result["vcs"] = vcs.get("vcs")
        result["requestedRevision"] = vcs.get("requested_revision")
        result["commit"] = vcs.get("commit_id")
    elif isinstance(directory, dict):
        result["installType"] = "editable" if directory.get("editable") else "directory"
    elif isinstance(archive, dict):
        result["installType"] = "archive"
    else:
        result["installType"] = "direct"
    return result


def print_installation_provenance(as_json=False):
    result = installation_provenance()
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"Super Codex {result['version']}")
    print(f"Executable: {result['executable']}")
    print(f"Install type: {result['installType']}")
    if result["source"]:
        print(f"Source: {result['source']}")
    if result["requestedRevision"]:
        print(f"Requested revision: {result['requestedRevision']}")
    if result["commit"]:
        print(f"Commit: {result['commit']}")
