#!/usr/bin/env python3
"""Preserve the active Spotify volume when playback moves to Soloist."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess  # nosec B404
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from bridge_config import PROJECT

# All subprocess calls use fixed local argv and never invoke a shell.
MPRIS_NAME = "org.mpris.MediaPlayer2.spotify"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_INTERFACE = "org.mpris.MediaPlayer2.Player"
BUSCTL = "/usr/bin/busctl"
DOCKER = "/usr/bin/docker"
SOLOIST_CONTAINER = "wiim-pc-bridge-soloist"
SOLOIST_WS = "127.0.0.1:9090"
VOLUME_FILE = PROJECT / "cache/soloist/data/handoff-volume"
DEFAULT_VOLUME = 40
SAMPLE_INTERVAL = 0.25
SETTLE_SECONDS = 0.6
RECONNECT_SECONDS = 2.0

stop_requested = False
trace_process: subprocess.Popen[str] | None = None


def parse_mpris_volume(output: str) -> int:
    """Convert busctl's `d 0.5` response to a Spotify percentage."""
    fields = output.split()
    if len(fields) != 2 or fields[0] != "d":
        raise ValueError(f"unexpected MPRIS volume response: {output!r}")
    value = float(fields[1])
    if not 0 <= value <= 1:
        raise ValueError(f"MPRIS volume is outside 0.0-1.0: {value}")
    return round(value * 100)


def parse_trace_activity(line: str) -> bool | None:
    """Read an active-state update from a timestamped Soloist trace line."""
    start = line.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(line[start:])
    except json.JSONDecodeError:
        return None
    active = payload.get("is_active") if isinstance(payload, dict) else None
    return active if isinstance(active, bool) else None


@dataclass
class VolumeTracker:
    """Keep the last stable non-Soloist volume and detect real handoffs."""

    saved_volume: int | None
    active: bool | None = None
    pending_volume: int | None = None
    pending_since: float = 0.0

    def observe_activity(self, active: bool) -> int | None:
        previous = self.active
        self.active = active
        if previous is False and active:
            return self.saved_volume
        return None

    def observe_source_volume(self, volume: int, now: float) -> int | None:
        if not 0 <= volume <= 100:
            raise ValueError("Spotify volume must be between 0 and 100")
        if self.saved_volume is None:
            self.saved_volume = volume
            self.pending_volume = None
            return volume
        if volume == self.saved_volume:
            self.pending_volume = None
            return None
        if volume != self.pending_volume:
            self.pending_volume = volume
            self.pending_since = now
            return None
        if now - self.pending_since < SETTLE_SECONDS:
            return None
        self.saved_volume = volume
        self.pending_volume = None
        return volume


def load_saved_volume(path: Path = VOLUME_FILE) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if 0 <= value <= 100 else None


def save_volume(volume: int, path: Path = VOLUME_FILE) -> None:
    if not 0 <= volume <= 100:
        raise ValueError("Spotify volume must be between 0 and 100")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{volume}\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def read_mpris_volume() -> int | None:
    try:
        completed = subprocess.run(  # nosec B603
            [
                BUSCTL,
                "--user",
                "get-property",
                MPRIS_NAME,
                MPRIS_PATH,
                MPRIS_INTERFACE,
                "Volume",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return parse_mpris_volume(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def set_soloist_volume(volume: int) -> bool:
    command = [
        DOCKER,
        "exec",
        SOLOIST_CONTAINER,
        "soloist",
        "ctl",
        "volume",
        str(volume),
        "--ws",
        SOLOIST_WS,
    ]
    for _attempt in range(4):
        try:
            subprocess.run(  # nosec B603
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            if stop_requested:
                break
            time.sleep(0.15)
    return False


def request_stop(_signum: int, _frame: object) -> None:
    global stop_requested
    stop_requested = True
    if trace_process is not None:
        with suppress(ProcessLookupError):
            trace_process.terminate()


def effective_volume(saved_volume: int | None) -> int:
    return DEFAULT_VOLUME if saved_volume is None else saved_volume


def trace_command() -> list[str]:
    return [
        DOCKER,
        "exec",
        SOLOIST_CONTAINER,
        "soloist",
        "ctl",
        "trace",
        "--ws",
        SOLOIST_WS,
    ]


def monitor_trace(tracker: VolumeTracker) -> None:
    global trace_process
    trace_process = subprocess.Popen(  # nosec B603
        trace_command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if trace_process.stdout is None:
        raise RuntimeError("could not read Soloist trace output")

    selector = selectors.DefaultSelector()
    selector.register(trace_process.stdout, selectors.EVENT_READ)
    next_sample = time.monotonic() + SAMPLE_INTERVAL
    mpris_missing_reported = False
    try:
        while not stop_requested:
            timeout = max(0.0, min(SAMPLE_INTERVAL, next_sample - time.monotonic()))
            for key, _mask in selector.select(timeout):
                line = key.fileobj.readline()
                if not line:
                    return
                active = parse_trace_activity(line)
                if active is None:
                    continue
                desired = tracker.observe_activity(active)
                if desired is not None:
                    if set_soloist_volume(desired):
                        print(
                            f"Preserved Spotify volume at {desired}% for the handoff.",
                            flush=True,
                        )
                    else:
                        print(
                            f"Could not preserve Spotify volume at {desired}%.",
                            file=sys.stderr,
                            flush=True,
                        )

            now = time.monotonic()
            if now < next_sample:
                continue
            next_sample = now + SAMPLE_INTERVAL
            if tracker.active is not False:
                continue
            volume = read_mpris_volume()
            if volume is None:
                if not mpris_missing_reported:
                    print(
                        "Spotify desktop volume is unavailable; using the last saved "
                        f"level ({effective_volume(tracker.saved_volume)}%).",
                        file=sys.stderr,
                        flush=True,
                    )
                    mpris_missing_reported = True
                continue
            mpris_missing_reported = False
            stable = tracker.observe_source_volume(volume, now)
            if stable is not None:
                save_volume(stable)
                print(f"Remembered Spotify source volume at {stable}%.", flush=True)
    finally:
        selector.close()
        if trace_process.poll() is None:
            trace_process.terminate()
        try:
            trace_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            trace_process.kill()
            trace_process.wait(timeout=3)
        trace_process = None


def main() -> int:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    tracker = VolumeTracker(load_saved_volume())
    print(
        "Spotify volume handoff monitor started; "
        f"fallback is {effective_volume(tracker.saved_volume)}%.",
        flush=True,
    )
    while not stop_requested:
        try:
            monitor_trace(tracker)
        except (OSError, RuntimeError) as exc:
            if not stop_requested:
                print(f"Soloist trace unavailable: {exc}", file=sys.stderr, flush=True)
        if not stop_requested:
            tracker.active = None
            time.sleep(RECONNECT_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
