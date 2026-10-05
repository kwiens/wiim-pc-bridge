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

import audio_flow_check
import bridge
import output_control
from bridge_config import PROJECT

# All subprocess calls use fixed local argv and never invoke a shell.
MPRIS_NAME = "org.mpris.MediaPlayer2.spotify"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_INTERFACE = "org.mpris.MediaPlayer2.Player"
BUSCTL = "/usr/bin/busctl"
DOCKER = "/usr/bin/docker"
SOLOIST_CONTAINER = "wiim-pc-bridge-soloist"
SHAIRPORT_CONTAINER = "wiim-pc-bridge-shairport"
SOLOIST_WS = "127.0.0.1:9090"
VOLUME_FILE = PROJECT / "cache/soloist/data/handoff-volume"
DEFAULT_VOLUME = 40
SAMPLE_INTERVAL = 0.25
SETTLE_SECONDS = 0.6
RECONNECT_SECONDS = 2.0
FLOW_CHECK_INTERVAL = 30.0
FLOW_CHECK_SECONDS = 5
FLOW_FAILURE_LIMIT = 2
PLAYBACK_POLL_INTERVAL = 5.0
IDLE_DISCONNECT_SECONDS = 60.0
IDLE_PIPE_PAUSE_SECONDS = 5.0
RECEIVER_RECONNECT_SECONDS = 30
RECEIVER_ADVERTISEMENT_SECONDS = 5

stop_requested = False
trace_process: subprocess.Popen[str] | None = None


class PipelineStalled(RuntimeError):
    """The managed audio path repeatedly failed its live health probe."""


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

    def observe_activity(self, active: bool) -> ActivityChange:
        previous = self.active
        self.active = active
        return ActivityChange(
            handoff_volume=self.saved_volume if previous is False and active else None,
            became_inactive=previous is True and not active,
        )

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


@dataclass
class FlowFailureTracker:
    """Require repeat evidence before restarting an otherwise healthy stack."""

    consecutive: int = 0

    def observe(self, failed: bool) -> bool:
        self.consecutive = self.consecutive + 1 if failed else 0
        return self.consecutive >= FLOW_FAILURE_LIMIT


@dataclass
class IdleOutputController:
    """Manage active playback and quiet idle, honoring explicit manual intent."""

    enabled: bool = True
    released: bool = False
    idle_since: float | None = None
    retry_at: float = 0.0
    retry_delay: float = PLAYBACK_POLL_INTERVAL
    pipe_paused: bool = False

    def release(self) -> bool:
        if not self.enabled:
            return False
        with output_control.locked():
            if not output_control.automatic_enabled():
                return False
            items = bridge.outputs()
            selected_ids = {
                bridge.output_id(item) for item in items if item.get("selected") is True
            }
            if not selected_ids:
                self.released = True
                return False
            pair_ids = {bridge.output_id(item) for item in bridge.resolve_pair(items)}
            if not selected_ids.issubset(pair_ids):
                return False
            bridge.set_outputs([])
            if any(item.get("selected") is True for item in bridge.outputs()):
                raise bridge.BridgeError("OwnTone did not release its idle outputs")
        self.released = True
        print("Spotify is idle; released the PC and WiiM AirPlay outputs.", flush=True)
        return True

    def observe(self, playing: bool, now: float) -> bool:
        if playing:
            self.idle_since = None
            self.pipe_paused = False
            if now < self.retry_at:
                return False
            try:
                with output_control.locked():
                    if not output_control.automatic_enabled():
                        return False
                    items = bridge.outputs()
                    pair_ids = {
                        bridge.output_id(item) for item in bridge.resolve_pair(items)
                    }
                    selected_ids = {
                        bridge.output_id(item)
                        for item in items
                        if item.get("selected") is True
                    }
                    # Leave unrelated user-selected outputs alone. An incomplete
                    # managed pair, however, may be a network failure, not a stop.
                    if not selected_ids.issubset(pair_ids):
                        return False
                    reconnected = selected_ids != pair_ids
                    if reconnected:
                        bridge.reconcile_once()
                    resumed = bridge.resume_pcm_playback()
                self.released = False
                self.retry_at = 0.0
                self.retry_delay = PLAYBACK_POLL_INTERVAL
                if reconnected or resumed:
                    print(
                        "Recovered active Spotify playback: "
                        f"outputs reconnected={reconnected}, PCM player resumed={resumed}.",
                        flush=True,
                    )
                return reconnected or resumed
            except (OSError, RuntimeError, ValueError):
                self.retry_at = now + self.retry_delay
                self.retry_delay = min(60.0, self.retry_delay * 2)
                raise
        self.retry_at = 0.0
        self.retry_delay = PLAYBACK_POLL_INTERVAL
        if not self.enabled:
            return False
        if self.idle_since is None:
            self.idle_since = now
        if not self.pipe_paused and now - self.idle_since >= IDLE_PIPE_PAUSE_SECONDS:
            with output_control.locked():
                if output_control.automatic_enabled():
                    bridge.pause_pcm_playback()
                    self.pipe_paused = True
        if now - self.idle_since >= IDLE_DISCONNECT_SECONDS:
            self.release()
        return False


@dataclass(frozen=True)
class ActivityChange:
    """State edge emitted by Soloist's Spotify Connect activity trace."""

    handoff_volume: int | None = None
    became_inactive: bool = False


@dataclass(frozen=True)
class OutputSnapshot:
    """The local output state that must survive a receiver recycle."""

    selected_ids: tuple[str, ...]
    local_id: str
    local_volume: int
    local_offset_ms: int


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


def capture_output_snapshot() -> OutputSnapshot | None:
    items = bridge.outputs()
    local = bridge.resolve_target(items, "local")
    if local.get("selected") is not True:
        return None
    return OutputSnapshot(
        selected_ids=tuple(
            bridge.output_id(item) for item in items if item.get("selected") is True
        ),
        local_id=bridge.output_id(local),
        local_volume=bridge.output_integer(local, "volume"),
        local_offset_ms=bridge.output_integer(local, "offset_ms"),
    )


def restore_output_snapshot(snapshot: OutputSnapshot) -> None:
    items = bridge.outputs()
    by_id = {bridge.output_id(item): item for item in items}
    missing = set(snapshot.selected_ids).difference(by_id)
    if missing or snapshot.local_id not in by_id:
        raise bridge.BridgeError("restarted local receiver did not return to OwnTone")
    local = by_id[snapshot.local_id]
    # A rediscovered receiver starts at OwnTone's default 50%. Restore its
    # prior level and offset before selecting it to avoid an audible burst.
    bridge.set_output_volume(local, snapshot.local_volume)
    bridge.set_output_offset(local, snapshot.local_offset_ms)
    bridge.set_outputs([by_id[identifier] for identifier in snapshot.selected_ids])
    # OwnTone can replace a permanent receiver's cached device object while
    # connecting it, which resets the just-applied value to 50%. Reapply after
    # selection while the source is idle, then verify the complete restoration.
    bridge.set_output_volume(local, snapshot.local_volume)
    bridge.set_output_offset(local, snapshot.local_offset_ms)
    refreshed = bridge.outputs()
    refreshed_by_id = {bridge.output_id(item): item for item in refreshed}
    selected_ids = {
        bridge.output_id(item) for item in refreshed if item.get("selected") is True
    }
    refreshed_local = refreshed_by_id.get(snapshot.local_id)
    if (
        selected_ids != set(snapshot.selected_ids)
        or refreshed_local is None
        or bridge.output_integer(refreshed_local, "volume") != snapshot.local_volume
        or bridge.output_integer(refreshed_local, "offset_ms")
        != snapshot.local_offset_ms
    ):
        raise bridge.BridgeError("OwnTone did not restore the local output state")


def recycle_local_receiver() -> bool:
    """Discard buffered local audio after Spotify moves to another device."""
    snapshot = capture_output_snapshot()
    if snapshot is None:
        return False
    subprocess.run(  # nosec B603
        [DOCKER, "restart", "--time", "10", SHAIRPORT_CONTAINER],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The container is running when `docker restart` returns, but its Avahi
    # advertisement is asynchronous. Let OwnTone observe that identity before
    # restoring the exact selection it held before the restart.
    time.sleep(RECEIVER_ADVERTISEMENT_SECONDS)
    restore_output_snapshot(snapshot)
    if not audio_flow_check.wait_for_local_receiver_route(RECEIVER_RECONNECT_SECONDS):
        raise RuntimeError("local Shairport route did not return after recycling")
    return True


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
                change = tracker.observe_activity(active)
                desired = change.handoff_volume
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
