#!/usr/bin/env python3
"""Run both privacy and secret scans over the exact objects being pushed."""

import io
import sys

import check_sensitive_data
import run_gitleaks


def main(input_stream=None):
    push_spec = (input_stream or sys.stdin).read()
    privacy_status = check_sensitive_data.main(
        ["--pre-push"], input_stream=io.StringIO(push_spec)
    )
    if privacy_status:
        return privacy_status
    return run_gitleaks.main(["--pre-push"], input_stream=io.StringIO(push_spec))


if __name__ == "__main__":
    raise SystemExit(main())
