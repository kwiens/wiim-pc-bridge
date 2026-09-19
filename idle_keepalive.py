#!/usr/bin/env python3
"""Opt-in, volume-independent idle audio for signal-sensing powered speakers."""

from __future__ import annotations

import argparse
import array
import fcntl
import json
import math
import os
import signal
import subprocess  # nosec B404
import sys
import threading
import time

import bridge
from bridge_config import BridgeConfig, ConfigError, get_config

RATE = 44100
CHANNELS = 2
BLOCK_BYTES = RATE * CHANNELS * 2 // 10
ACTIVITY_PEAK = 4
QUIET_SECONDS = 5
RETRY_SECONDS = 30
PAREC = "/usr/bin/parec"
PACAT = "/usr/bin/pacat"
BUSCTL = "/usr/bin/busctl"
DOCKER = "/usr/bin/docker"


def pcm_peak(data: bytes) -> int:
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    return max(map(abs, samples), default=0)


def make_burst(duration_seconds: int, level_db: int) -> bytes:
    """500 Hz, stereo PCM16, with 200 ms cosine fades and zero endpoints."""
    frames = RATE * duration_seconds
    fade_frames = RATE // 5
    amplitude = 32767 * 10 ** (level_db / 20)
    samples = array.array("h")
    for frame in range(frames):
        edge = min(frame, frames - 1 - frame, fade_frames)
        envelope = (1 - math.cos(math.pi * edge / fade_frames)) / 2
        value = round(amplitude * envelope * math.sin(2 * math.pi * 500 * frame / RATE))
        samples.extend((value, value))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class ActivityMonitor:
    """Continuously drain a separate monitor tap; never read the OwnTone FIFO."""

    def __init__(self, sink: str) -> None:
        self.last_audio = time.monotonic()
        self.ready = threading.Event()
        self.error: str | None = None
        self.process = subprocess.Popen(  # nosec B603
            [
                PAREC,
                "--device=" + sink + ".monitor",
                "--format=s16le",
                "--rate=44100",
                "--channels=2",
                "--latency-msec=100",
                "--process-time-msec=20",
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        pending = b""
        try:
            while data := self.process.stdout.read(BLOCK_BYTES):
                pending += data
                size = len(pending) - len(pending) % 2
                if size:
                    if pcm_peak(pending[:size]) >= ACTIVITY_PEAK:
                        self.last_audio = time.monotonic()
                    self.ready.set()
                    pending = pending[size:]
            self.error = "PipeWire monitor closed"
        except OSError as exc:
            self.error = str(exc)

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.thread.join(timeout=1)
        if self.process.stdout is not None:
            self.process.stdout.close()


def spotify_playing() -> bool:
    """Supplement PCM detection when the desktop client is playing muted audio."""
    result = subprocess.run(  # nosec B603
        [
            BUSCTL,
            "--user",
            "get-property",
            "org.mpris.MediaPlayer2.spotify",
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player",
            "PlaybackStatus",
        ],
        capture_output=True,
        text=True,
        timeout=3,
    )
    return result.returncode == 0 and result.stdout.strip() == 's "Playing"'


def soloist_playing() -> bool:
    """Use Soloist's real playback state, including on a headless/muted bridge."""
    result = subprocess.run(  # nosec B603
        [
            DOCKER,
            "exec",
            "wiim-pc-bridge-soloist",
            "soloist",
            "ctl",
            "now",
            "--json",
            "--ws",
            "127.0.0.1:9090",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode:
        raise RuntimeError("Soloist playback state unavailable")
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Soloist playback state is not JSON") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("is_active"), bool)
        or not isinstance(data.get("status"), str)
    ):
        raise RuntimeError("Soloist playback state is unknown")
    return data["is_active"] and data["status"] not in ("paused", "stopped")


def idle_guard(config: BridgeConfig) -> str | None:
    """Read-only checks: never select outputs, change volumes, or switch sources."""
    wiim = bridge.resolve_target(bridge.outputs(), "wiim")
    if wiim.get("selected") is not True:
        return "WiiM output is disconnected; reconnect explicitly to resume"
    if spotify_playing() or soloist_playing():
        return "Spotify reports active playback"
    bridge.validate_wiim_group()
    nodes = [(config.kitchen_ip, False)]
    nodes.extend((ip, True) for ip, _name in config.followers)
    for ip, follower in nodes:
        status = bridge.wiim_request(ip, "getPlayerStatus")
        state = status.get("status")
        mode = str(status.get("mode", ""))
        # A selected OwnTone session continuously plays PCM, including silence.
        # Permit that existing transport, but never reopen it or take another
        # source over. Other playback states (including loading) fail closed.
        own_transport = state == "play" and mode == ("99" if follower else "1")
        if state not in ("stop", "pause") and not own_transport:
            return "WiiM reports another source playing or an unknown playback state"
    return None


def play_burst(config: BridgeConfig, data: bytes) -> None:
    # The generated PCM sets the level. This is a separate Pulse stream at
    # unity gain: no Spotify, sink, WiiM, or OwnTone volume is written.
    process = subprocess.Popen(  # nosec B603
        [
            PACAT,
            "--playback",
            "--raw",
            "--device=" + config.bridge_sink,
            "--format=s16le",
            "--rate=44100",
            "--channels=2",
            "--volume=65536",
            "--client-name=WiiM idle keepalive",
            "--stream-name=Idle keepalive",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _output, error = process.communicate(
            data, timeout=config.keepalive_duration_seconds + 10
        )
        if process.returncode:
            raise RuntimeError(
                "Keepalive playback failed: " + error.decode(errors="replace").strip()
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def run(config: BridgeConfig, stop: threading.Event, once: bool = False) -> int:
    if not config.keepalive_enabled and not once:
        print("Idle keepalive disabled in .env.", flush=True)
        return 0
    burst = make_burst(config.keepalive_duration_seconds, config.keepalive_level_db)
    monitor = ActivityMonitor(config.bridge_sink)
    retry_at = 0.0
    previous_reason = None
    started = time.monotonic()
    print(
        f"Idle keepalive: {config.keepalive_duration_seconds}s faded 500 Hz tone "
        f"at {config.keepalive_level_db} dBFS every "
        f"{config.keepalive_interval_seconds}s without signal. "
        "Spotify volume is unchanged; selected PC output also receives the tone.",
        flush=True,
    )
    try:
        while not stop.is_set():
            now = time.monotonic()
            if once and now - started > 15:
                print("Keepalive skipped: no quiet test window within 15s.", flush=True)
                return 1
            if monitor.error:
                raise RuntimeError(monitor.error)
            if not monitor.ready.is_set() and now - monitor.last_audio > 10:
                raise RuntimeError("No PCM arrived from the PipeWire monitor")
            interval = QUIET_SECONDS if once else config.keepalive_interval_seconds
            if (
                not monitor.ready.is_set()
                or now - monitor.last_audio < interval
                or now < retry_at
            ):
                stop.wait(0.2 if once else 1)
                continue
            try:
                reason = idle_guard(config)
            except (
                bridge.BridgeError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                reason = "Safety check unavailable: " + str(exc)
            # Recheck PCM after LAN queries so playback starting during them
            # cancels this attempt. Sampled activity is conservative (4 LSB).
            if time.monotonic() - monitor.last_audio < QUIET_SECONDS:
                reason = "Audio appeared during the safety checks"
            if stop.is_set():
                return 0
            if reason:
                if reason != previous_reason:
                    print("Keepalive skipped: " + reason, flush=True)
                if once:
                    return 1
                previous_reason = reason
                retry_at = time.monotonic() + RETRY_SECONDS
                continue
            play_burst(config, burst)
            monitor.last_audio = time.monotonic()
            retry_at = monitor.last_audio + config.keepalive_interval_seconds
            previous_reason = None
            print(
                "Keepalive sent; next burst after "
                f"{config.keepalive_interval_seconds}s of inactivity.",
                flush=True,
            )
            if once:
                return 0
    finally:
        monitor.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="send one guarded test burst after 5s of silence (even if disabled)",
    )
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    try:
        config = get_config()
        flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW
        descriptor = os.open(
            config.runtime_dir / "wiim-pc-bridge-keepalive.lock", flags, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return run(config, stop, once=args.once)
        finally:
            os.close(descriptor)
    except (
        ConfigError,
        bridge.BridgeError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        print("Idle keepalive: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
