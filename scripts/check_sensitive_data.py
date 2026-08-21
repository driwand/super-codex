#!/usr/bin/env python3
"""Block likely credentials, private identities, and local paths from Git."""

import argparse
import getpass
import hashlib
import itertools
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


LOCAL_HASH_FILE = ".sensitive-hashes.local"
HASH_ENVIRONMENT = "SUPER_CODEX_SENSITIVE_TERM_DIGESTS"
ZERO_SHA = "0" * 40
MAX_BLOB_BYTES = 16 * 1024 * 1024
TOKEN_RE = re.compile(r"[0-9A-Za-z]+")
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,}\b"
)
HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s'\"]+")


class ScanError(RuntimeError):
    pass


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    rule: str


def run_git(arguments, input_value=None, text=True):
    try:
        result = subprocess.run(
            ["git"] + list(arguments),
            input=input_value,
            capture_output=True,
            text=text,
            check=False,
        )
    except OSError as exc:
        raise ScanError(f"cannot run Git: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() if text else result.stderr.decode(errors="replace").strip()
        raise ScanError(detail or f"Git command failed: {' '.join(arguments)}")
    return result.stdout


def repository_root():
    return Path(run_git(["rev-parse", "--show-toplevel"]).strip())


def normalize_sensitive_term(value):
    return " ".join(TOKEN_RE.findall(value.casefold()))


def term_digest(value):
    normalized = normalize_sensitive_term(value)
    if not normalized:
        raise ScanError("sensitive term must contain a letter or number")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_path(root):
    return root / LOCAL_HASH_FILE


def validate_digest(value, source):
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ScanError(f"invalid sensitive-term digest in {source}")
    return normalized


def load_sensitive_hashes(root):
    path = hash_path(root)
    hashes = set()
    try:
        details = path.lstat()
    except FileNotFoundError:
        details = None
    if details is not None:
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ScanError(f"refusing unsafe private hash file: {LOCAL_HASH_FILE}")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise ScanError(f"private hash file is owned by another user: {LOCAL_HASH_FILE}")
        if details.st_mode & 0o077:
            raise ScanError(f"private hash file permissions are too broad: {LOCAL_HASH_FILE}")
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            value = raw.strip().lower()
            if not value or value.startswith("#"):
                continue
            hashes.add(validate_digest(value, f"{LOCAL_HASH_FILE}:{number}"))
    for number, raw in enumerate(os.environ.get(HASH_ENVIRONMENT, "").splitlines(), 1):
        value = raw.strip()
        if value:
            hashes.add(validate_digest(value, f"{HASH_ENVIRONMENT}:{number}"))
    return hashes


def register_sensitive_term(root):
    value = getpass.getpass("Sensitive term (input hidden): ")
    digest = term_digest(value)
    hashes = load_sensitive_hashes(root)
    hashes.add(digest)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".sensitive-hashes.", suffix=".tmp", dir=str(root)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            for item in sorted(hashes):
                handle.write(item + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, hash_path(root))
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    print(f"Registered one private sensitive-term digest in {LOCAL_HASH_FILE}.")


def candidate_digests(value):
    tokens = TOKEN_RE.findall(value.casefold())
    for start in range(len(tokens)):
        for width in range(1, min(6, len(tokens) - start) + 1):
            phrase = " ".join(tokens[start : start + width])
            yield hashlib.sha256(phrase.encode("utf-8")).hexdigest()


def contains_sensitive_term(value, sensitive_hashes):
    return bool(sensitive_hashes) and any(
        digest in sensitive_hashes for digest in candidate_digests(value)
    )


def safe_email(value):
    lowered = value.casefold()
    return (
        lowered == "git@github.com"
        or lowered == "noreply@github.com"
        or lowered == "noreply@anthropic.com"
        or lowered == "secret-token@github.com"
        or lowered.endswith("@example.com")
        or lowered.endswith("@example.org")
        or lowered.endswith("@example.net")
        or lowered.endswith("@users.noreply.github.com")
    )


def safe_source(source, sensitive_hashes):
    if contains_sensitive_term(source, sensitive_hashes):
        return "<redacted-sensitive-path>"
    return source.replace("\n", "\\n")


def scan_content(source, data, sensitive_hashes):
    display_source = safe_source(source, sensitive_hashes)
    findings = []
    if Path(source.split("@", 1)[0]).name == LOCAL_HASH_FILE:
        findings.append(Finding(display_source, 0, "private-scan-config-must-not-be-tracked"))
    if contains_sensitive_term(source, sensitive_hashes):
        findings.append(Finding(display_source, 0, "private-sensitive-term-in-path"))
    if len(data) > MAX_BLOB_BYTES:
        findings.append(Finding(display_source, 0, "file-too-large-to-scan"))
        return findings
    text = data.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), 1):
        if contains_sensitive_term(line, sensitive_hashes):
            findings.append(Finding(display_source, number, "private-sensitive-term"))
        if HOME_PATH_RE.search(line):
            findings.append(Finding(display_source, number, "absolute-user-home-path"))
        for match in EMAIL_RE.finditer(line):
            if not safe_email(match.group(0)):
                findings.append(Finding(display_source, number, "email-address"))
    return findings


def nul_paths(arguments):
    output = run_git(arguments, text=False)
    return [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]


def tracked_sources(root):
    for path in nul_paths(["ls-files", "-z"]):
        candidate = root / path
        if candidate.is_file() and not candidate.is_symlink():
            yield path, candidate.read_bytes()


def working_tree_sources(root):
    paths = nul_paths(["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    for path in paths:
        candidate = root / path
        if candidate.is_file() and not candidate.is_symlink():
            yield path, candidate.read_bytes()


def staged_sources():
    paths = nul_paths(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", "--"]
    )
    for path in paths:
        try:
            data = run_git(["show", f":{path}"], text=False)
        except ScanError as exc:
            raise ScanError(f"cannot read staged file {path!r}: {exc}") from exc
        yield path, data


def object_sources(revision_arguments):
    output = run_git(["rev-list", "--objects"] + list(revision_arguments))
    objects = {}
    for line in output.splitlines():
        object_id, separator, path = line.partition(" ")
        objects.setdefault(object_id, path if separator else "")
    if not objects:
        return
    query = "".join(object_id + "\n" for object_id in objects)
    details = run_git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        input_value=query,
    )
    for line in details.splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[1] != "blob":
            continue
        object_id, _, size_text = fields
        path = objects.get(object_id) or "<unknown-path>"
        size = int(size_text)
        source = f"{path}@{object_id[:12]}"
        if size > MAX_BLOB_BYTES:
            yield source, b"x" * (MAX_BLOB_BYTES + 1)
        else:
            yield source, run_git(["cat-file", "blob", object_id], text=False)


def history_sources():
    return itertools.chain(object_sources(["--all"]), commit_sources(["--all"]))


def commit_sources(revision_arguments):
    commits = run_git(["rev-list"] + list(revision_arguments)).splitlines()
    for commit_id in commits:
        if not re.fullmatch(r"[0-9a-f]{40}", commit_id):
            raise ScanError("Git returned an invalid commit object ID")
        yield f"<commit-metadata>@{commit_id[:12]}", run_git(
            ["cat-file", "commit", commit_id], text=False
        )


def pre_push_sources(stream):
    positive = []
    negative = []
    for raw in stream:
        fields = raw.split()
        if len(fields) != 4:
            raise ScanError("invalid pre-push hook input")
        _, local_sha, _, remote_sha = fields
        if local_sha == ZERO_SHA:
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", local_sha):
            raise ScanError("invalid local object ID in pre-push input")
        positive.append(local_sha)
        if remote_sha != ZERO_SHA:
            if not re.fullmatch(r"[0-9a-f]{40}", remote_sha):
                raise ScanError("invalid remote object ID in pre-push input")
            negative.append(remote_sha)
    if not positive:
        return iter(())
    arguments = positive + (["--not"] + negative if negative else [])
    return itertools.chain(object_sources(arguments), commit_sources(arguments))


def unique_findings(sources, sensitive_hashes):
    findings = set()
    for source, data in sources:
        findings.update(scan_content(source, data, sensitive_hashes))
    return sorted(findings, key=lambda item: (item.source, item.line, item.rule))


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    modes = value.add_mutually_exclusive_group()
    modes.add_argument(
        "--working-tree",
        action="store_true",
        help="scan tracked and non-ignored untracked working files",
    )
    modes.add_argument("--tracked", action="store_true", help="scan tracked working files")
    modes.add_argument("--staged", action="store_true", help="scan the staged Git snapshot")
    modes.add_argument("--history", action="store_true", help="scan all reachable Git blobs")
    modes.add_argument(
        "--pre-push", action="store_true", help="scan objects described on standard input"
    )
    modes.add_argument(
        "--register-sensitive-term",
        action="store_true",
        help="privately register a term by its digest",
    )
    value.add_argument(
        "--allow-empty-terms",
        action="store_true",
        help="run generic checks without any private sensitive-term digest",
    )
    return value


def main(argv=None, input_stream=None):
    args = parser().parse_args(argv)
    input_stream = input_stream or sys.stdin
    try:
        root = repository_root()
        if args.register_sensitive_term:
            register_sensitive_term(root)
            return 0
        sensitive_hashes = load_sensitive_hashes(root)
        if not sensitive_hashes and not args.allow_empty_terms:
            raise ScanError(
                "no private sensitive-term digest configured; register one locally or "
                f"set {HASH_ENVIRONMENT}"
            )
        if args.staged:
            sources = staged_sources()
        elif args.history:
            sources = history_sources()
        elif args.pre_push:
            sources = pre_push_sources(input_stream)
        elif args.tracked:
            sources = tracked_sources(root)
        else:
            sources = working_tree_sources(root)
        findings = unique_findings(sources, sensitive_hashes)
    except (OSError, UnicodeError, ValueError, ScanError) as exc:
        print(f"sensitive-data scan failed: {exc}", file=sys.stderr)
        return 2
    if not findings:
        print("Sensitive-data scan passed.")
        return 0
    print(
        f"Sensitive-data scan blocked {len(findings)} finding(s); values are redacted.",
        file=sys.stderr,
    )
    for finding in findings:
        location = f"{finding.source}:{finding.line}" if finding.line else finding.source
        print(f"{location}: {finding.rule} [REDACTED]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
