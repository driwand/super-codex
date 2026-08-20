#!/usr/bin/env python3
"""Build the dependency-free Super Codex release executable."""

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
import tempfile
import zipapp
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from super_agent import __version__  # noqa: E402


TAG_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def build(tag, commit, output_directory):
    if not TAG_PATTERN.fullmatch(tag) or tag != f"v{__version__}":
        raise ValueError(f"tag must match package version v{__version__}")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("commit must be a full lowercase Git SHA")

    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / "sc"
    metadata = {
        "schemaVersion": 1,
        "installType": "standalone-release",
        "version": __version__,
        "tag": tag,
        "commit": commit,
        "source": f"https://github.com/driwand/super-codex/releases/tag/{tag}",
    }
    with tempfile.TemporaryDirectory() as temporary_name:
        staging = Path(temporary_name)
        shutil.copytree(
            SRC / "super_agent",
            staging / "super_agent",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_build.json"),
        )
        (staging / "super_agent" / "_build.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (staging / "__main__.py").write_text(
            "from super_agent.cli import main\nraise SystemExit(main())\n",
            encoding="utf-8",
        )
        zipapp.create_archive(
            staging,
            target=output,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    content = output.read_bytes()
    manifest = {
        "schemaVersion": 1,
        "version": __version__,
        "tag": tag,
        "commit": commit,
        "asset": "sc",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    (output_directory / "release.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return output, manifest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("commit")
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        output, _ = build(args.tag, args.commit, args.output)
    except ValueError as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
