"""Coordinate automatic recovery with explicit user output commands."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bridge_config import PROJECT, get_config


def control_directory() -> Path:
    return get_config().runtime_dir


def policy_path() -> Path:
    return PROJECT / "cache/output-policy.json"


def mode() -> str:
    try:
        data = json.loads(policy_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Preserve the old installation's explicit opt-out during migration.
        return (
            "manual"
            if (control_directory() / "wiim-pc-bridge-manual-outputs").exists()
            else "auto"
        )
    if not isinstance(data, dict) or data.get("mode") not in (
        "auto",
        "local",
        "stopped",
        "manual",
    ):
        raise ValueError("invalid output policy; refusing automatic output changes")
    return data["mode"]


def set_mode(value: str) -> None:
    if value not in ("auto", "local", "stopped", "manual"):
        raise ValueError("invalid output control mode")
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".output-policy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "mode": value}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked() -> Iterator[None]:
    """Serialize output changes across the CLI and background monitor."""
    descriptor = os.open(
        control_directory() / "wiim-pc-bridge-outputs.lock",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "a") as handle:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "another output operation held the bridge lock for 10s"
                    ) from None
                time.sleep(0.05)
        yield


def automatic_enabled() -> bool:
    # A lost connection and an intentional stop look identical in OwnTone's
    # API. Only explicit bridge CLI commands set this opt-out marker.
    return mode() == "auto"


def set_automatic(enabled: bool) -> None:
    """Compatibility helper; call with locked() held."""
    set_mode("auto" if enabled else "manual")
