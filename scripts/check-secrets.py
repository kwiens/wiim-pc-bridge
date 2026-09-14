#!/usr/bin/env python3
"""Fail if a committed credential looks reachable from this repository.

Publishing a repository publishes its whole history, so scanning only the
working tree is not enough: a key committed once and deleted later is still in
every clone. This scans the working tree *and* every blob reachable from any
ref, and additionally looks for the literal API key this installation uses, so
a key whose format differs from the patterns below is still caught.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

# subprocess invokes a resolved Git executable with fixed argv and no shell.

PROJECT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2 * 1024 * 1024

PATTERNS = {
    "Spotify Soloist API key": re.compile(rb"spak_[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS access key": re.compile(rb"AKIA[A-Z0-9]{16}"),
    # Covers PRIVATE KEY plus the RSA/EC/DSA/OPENSSH/ENCRYPTED/PGP variants.
    # Written as a pattern rather than a literal so this file does not match
    # itself, in the working tree or in history.
    "private key": re.compile(
        rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
    ),
}


def git() -> str:
    found = shutil.which("git")
    if found is None:
        raise RuntimeError("git is required for the credential scan")
    return found


def run(*args: str) -> bytes:
    completed = subprocess.run(  # nosec B603
        [git(), *args],
        cwd=PROJECT,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def working_tree_files() -> list[Path]:
    stdout = run("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [PROJECT / item.decode() for item in stdout.split(b"\0") if item]


def history_blobs() -> list[tuple[str, int]]:
    """Every blob reachable from any ref, newest objects included."""
    stdout = run(
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        "--batch-all-objects",
    )
    blobs: list[tuple[str, int]] = []
    for line in stdout.decode("ascii", "replace").splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1] == "blob":
            size = int(fields[2])
            if size <= MAX_BYTES:
                blobs.append((fields[0], size))
    return blobs


def blob_contents(shas: list[str]) -> dict[str, bytes]:
    """Read many blobs through one `git cat-file --batch` process."""
    if not shas:
        return {}
    completed = subprocess.run(  # nosec B603
        [git(), "cat-file", "--batch"],
        cwd=PROJECT,
        check=True,
        capture_output=True,
        input=("\n".join(shas) + "\n").encode("ascii"),
    )
    contents: dict[str, bytes] = {}
    stream = completed.stdout
    offset = 0
    while offset < len(stream):
        newline = stream.find(b"\n", offset)
        if newline < 0:
            break
        header = stream[offset:newline].split()
        offset = newline + 1
        if len(header) != 3:
            continue
        sha, size = header[0].decode("ascii"), int(header[2])
        contents[sha] = stream[offset : offset + size]
        offset += size + 1
    return contents


def live_key_values() -> list[bytes]:
    """The literal secret this installation uses, so format never matters."""
    candidates: list[bytes] = []
    key_path: Path | None = None
    env_file = PROJECT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            name, _, raw = line.partition("=")
            if name.strip() == "SOLOIST_KEY_FILE":
                key_path = Path(raw.strip().strip("\"'"))
                break
    if key_path is None:
        key_path = Path.home() / ".config/wiim-pc-bridge/soloist_api_key"
    try:
        value = key_path.read_bytes().strip()
    except OSError:
        return candidates
    # Too short to be a credential worth grepping for; avoid false positives.
    if len(value) >= 16:
        candidates.append(value)
    return candidates


def scan(content: bytes, literals: list[bytes]) -> list[str]:
    labels = [label for label, pattern in PATTERNS.items() if pattern.search(content)]
    if any(literal in content for literal in literals):
        labels.append("this installation's live API key")
    return labels


def main() -> int:
    literals = live_key_values()
    findings: list[str] = []

    for path in working_tree_files():
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            continue
        for label in scan(path.read_bytes(), literals):
            findings.append(
                f"possible {label} in working tree: {path.relative_to(PROJECT)}"
            )

    blobs = history_blobs()
    contents = blob_contents([sha for sha, _size in blobs])
    for sha, content in contents.items():
        for label in scan(content, literals):
            findings.append(f"possible {label} in Git history: blob {sha[:12]}")

    if findings:
        for line in sorted(set(findings)):
            print(line, file=sys.stderr)
        print(
            "\nA credential in history stays in every clone. Rotate the key and "
            "rewrite history before publishing.",
            file=sys.stderr,
        )
        return 1
    print(
        f"No credential patterns found in {len(contents)} historical blob(s) "
        "or the working tree."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
