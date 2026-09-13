#!/usr/bin/env python3
"""Fail if a Git candidate looks like a committed credential."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

# subprocess invokes a resolved Git executable with fixed argv and no shell.

PROJECT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "Spotify Soloist API key": re.compile(rb"spak_[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS access key": re.compile(rb"AKIA[A-Z0-9]{16}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def candidate_paths() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for the credential scan")
    completed = subprocess.run(  # nosec B603
        [
            git,
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=PROJECT,
        check=True,
        capture_output=True,
    )
    return [PROJECT / item.decode() for item in completed.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[tuple[Path, str]] = []
    for path in candidate_paths():
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        content = path.read_bytes()
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append((path.relative_to(PROJECT), label))
    if findings:
        for path, label in findings:
            print(f"possible {label}: {path}", file=sys.stderr)
        return 1
    print("No credential patterns found in Git candidate files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
