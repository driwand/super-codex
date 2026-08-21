#!/usr/bin/env python3
"""Run Gitleaks only after proving that its detection engine works."""

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


class GitleaksError(RuntimeError):
    pass


ZERO_SHA = "0" * 40


def executable(root):
    managed = root / ".tools" / "gitleaks"
    try:
        details = managed.lstat()
    except FileNotFoundError:
        details = None
    if details is not None:
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise GitleaksError("refusing unsafe managed Gitleaks executable")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise GitleaksError("managed Gitleaks executable is owned by another user")
        if details.st_mode & 0o022 or not details.st_mode & stat.S_IXUSR:
            raise GitleaksError("managed Gitleaks executable has unsafe permissions")
        return str(managed)
    located = shutil.which("gitleaks")
    if located:
        return located
    raise GitleaksError(
        "Gitleaks is missing; run `python3 scripts/install_gitleaks.py` before committing"
    )


def self_test(path, root):
    canaries = {
        "GitHub": "".join(("gh", "p_", "Q7m2Zp9Lx4Nc8Vb1Ks6Hd3Rf0Tj5Wy2Ea9Ug")),
        "Slack": "".join(
            ("xox", "b-", "123456789012-", "123456789012-", "abcdefghijklmnopqrstuvwx")
        ),
    }
    for rule, token in canaries.items():
        result = subprocess.run(
            [path, "stdin", "--no-banner", "--no-color", "--redact"],
            cwd=root,
            input=f'token = "{token}"\n',
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 1:
            raise GitleaksError(
                f"Gitleaks failed its {rule} detection self-test; run "
                "`python3 scripts/install_gitleaks.py` to install the pinned working version"
            )


def pre_push_log_options(stream):
    positive = []
    negative = []
    for raw in stream:
        fields = raw.split()
        if len(fields) != 4:
            raise GitleaksError("invalid pre-push hook input")
        _, local_sha, _, remote_sha = fields
        if local_sha == ZERO_SHA:
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", local_sha):
            raise GitleaksError("invalid local object ID in pre-push input")
        positive.append(local_sha)
        if remote_sha != ZERO_SHA:
            if not re.fullmatch(r"[0-9a-f]{40}", remote_sha):
                raise GitleaksError("invalid remote object ID in pre-push input")
            negative.append(remote_sha)
    if not positive:
        return None
    revisions = positive + (["--not"] + negative if negative else [])
    return " ".join(revisions)


def scan(root, staged=False, log_options=None):
    path = executable(root)
    self_test(path, root)
    command = [path, "git"]
    if staged:
        command.append("--staged")
    elif log_options:
        command.append(f"--log-opts={log_options}")
    else:
        command.append("--log-opts=--all")
    command.extend(["--no-banner", "--no-color", "--redact", "."])
    result = subprocess.run(command, cwd=root, check=False)
    if result.returncode == 0:
        print("Gitleaks scan passed.")
    return result.returncode


def main(argv=None, input_stream=None):
    value = argparse.ArgumentParser(description=__doc__)
    modes = value.add_mutually_exclusive_group(required=True)
    modes.add_argument("--staged", action="store_true")
    modes.add_argument("--history", action="store_true")
    modes.add_argument("--pre-push", action="store_true")
    args = value.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.pre_push:
            log_options = pre_push_log_options(input_stream or sys.stdin)
            if log_options is None:
                print("Gitleaks scan passed (no pushed objects).")
                return 0
            return scan(root, log_options=log_options)
        return scan(root, staged=args.staged)
    except (OSError, GitleaksError) as exc:
        print(f"Gitleaks scan failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
