#!/usr/bin/env python3
"""Configure bridge levels at startup without waking idle AirPlay speakers."""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
import time

import audio_flow_check
import bridge
from bridge_config import ConfigError, get_config


def main() -> int:
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                playing = audio_flow_check.soloist_has_active_playback()
                break
            except (
                OSError,
                RuntimeError,
                ValueError,
                json.JSONDecodeError,
                subprocess.SubprocessError,
            ):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        bridge.reconcile(
            180, activate=playing or get_config().keepalive_enabled, respect_manual=True
        )
    except (
        bridge.BridgeError,
        ConfigError,
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Startup reconciliation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
