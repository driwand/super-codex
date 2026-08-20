#!/usr/bin/env python3
"""Verify release version consistency without third-party dependencies."""

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from super_agent import __version__  # noqa: E402


def project_version(text):
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = re.fullmatch(r'version\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    raise ValueError("pyproject.toml has no [project] version")


def validate(tag=None):
    declared = project_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    errors = []
    if declared != __version__:
        errors.append(
            f"pyproject.toml version {declared} does not match package version {__version__}"
        )
    expected_tag = f"v{declared}"
    if tag is not None and tag != expected_tag:
        errors.append(f"tag {tag} does not match package version {expected_tag}")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{declared}]" not in changelog:
        errors.append(f"CHANGELOG.md has no release section for {declared}")
    return errors


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print("usage: check_release_version.py [vVERSION]", file=sys.stderr)
        return 2
    errors = validate(arguments[0] if arguments else None)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Release version is consistent: v{__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
