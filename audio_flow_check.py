#!/usr/bin/env python3
"""Verify active Spotify PCM reaches the local bridge output."""

from __future__ import annotations

import argparse
import array
import json
import os
import selectors
import stat
import subprocess  # nosec B404
import sys
import time

import bridge
from bridge_config import ConfigError, get_config

DOCKER = "/usr/bin/docker"
PACTL = "/usr/bin/pactl"
PAREC = "/usr/bin/parec"
SOLOIST_CONTAINER = "wiim-pc-bridge-soloist"
SOLOIST_WS = "127.0.0.1:9090"
# Fixed path inside the pinned Shairport image, not a host temporary file.
SHAIRPORT_PULSE_SOCKET = "/tmp/pulseaudio.socket"  # nosec B108
ACTIVE_PEAK = 4
SAMPLE_SECONDS = 1
SOCKET_STAT = "%d:%i"


def pcm_peak(data: bytes) -> int:
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    return max(map(abs, samples), default=0)


def soloist_has_active_playback() -> bool:
    result = subprocess.run(  # nosec B603
        [
            DOCKER,
            "exec",
            SOLOIST_CONTAINER,
            "soloist",
            "ctl",
            "now",
            "--json",
            "--ws",
            SOLOIST_WS,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    data = json.loads(result.stdout)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("is_active"), bool)
        or not isinstance(data.get("status"), str)
    ):
        raise RuntimeError("Soloist playback state is unknown")
    return data["is_active"] and data["status"] in ("playing", "buffering")


def socket_identity(path: str) -> tuple[int, int]:
    info = os.stat(path)
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"audio endpoint is not a socket: {path}")
    return info.st_dev, info.st_ino


def container_socket_identity(container: str, path: str) -> tuple[int, int]:
    result = subprocess.run(  # nosec B603
        [DOCKER, "exec", container, "stat", "-Lc", SOCKET_STAT, path],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    fields = result.stdout.strip().split(":")
    if len(fields) != 2:
        raise RuntimeError(f"could not identify {path} in {container}")
    return int(fields[0]), int(fields[1])


def runtime_socket_mounts_current() -> bool:
    """Detect socket-file bind mounts made stale by a host audio restart."""
    config = get_config()
    pulse_socket = str(config.runtime_dir / "pulse/native")
    pipewire_socket = str(config.runtime_dir / "pipewire-0")
    try:
        pulse_identity = socket_identity(pulse_socket)
        pipewire_identity = socket_identity(pipewire_socket)
        return (
            container_socket_identity(SOLOIST_CONTAINER, pulse_socket) == pulse_identity
            and container_socket_identity(SOLOIST_CONTAINER, pipewire_socket)
            == pipewire_identity
            and container_socket_identity(
                "wiim-pc-bridge-shairport", SHAIRPORT_PULSE_SOCKET
            )
            == pulse_identity
            and container_socket_identity("wiim-pc-bridge-capture", pulse_socket)
            == pulse_identity
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False


def sample_peaks(devices: tuple[str, ...]) -> dict[str, int]:
    """Capture all monitor devices concurrently for one second."""
    processes: dict[str, subprocess.Popen[bytes]] = {}
    selector = selectors.DefaultSelector()
    chunks = {device: bytearray() for device in devices}
    try:
        for device in devices:
            process = subprocess.Popen(  # nosec B603
                [
                    PAREC,
                    "--device=" + device,
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
            if process.stdout is None:
                raise RuntimeError(f"could not read PipeWire monitor {device}")
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, device)
            processes[device] = process

        deadline = time.monotonic() + SAMPLE_SECONDS
        while selector.get_map() and time.monotonic() < deadline:
            for key, _mask in selector.select(max(0.0, deadline - time.monotonic())):
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    # Readiness can disappear between select() and read().
                    continue
                if data:
                    chunks[key.data].extend(data)
                else:
                    selector.unregister(key.fileobj)
        return {device: pcm_peak(data) for device, data in chunks.items()}
    finally:
        selector.close()
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.stdout is not None:
                process.stdout.close()


def announce(message: str, *, verbose: bool, error: bool = False) -> None:
    if verbose:
        print(message, file=sys.stderr if error else sys.stdout, flush=True)


def configured_outputs_selected() -> bool:
    """Return whether OwnTone is intentionally driving the configured pair."""
    items = bridge.outputs()
    local, wiim = bridge.resolve_pair(items)
    expected_ids = {
        bridge.output_id(local),
        bridge.output_id(wiim),
    }
    selected_ids = {
        bridge.output_id(item) for item in items if item.get("selected") is True
    }
    return selected_ids == expected_ids


def pulse_objects(object_type: str) -> list[dict[str, object]]:
    """Return one typed Pulse object list from pactl's machine-readable output."""
    result = subprocess.run(  # nosec B603
        [PACTL, "-f", "json", "list", object_type],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise RuntimeError(f"pactl returned malformed {object_type} JSON")
    return payload


def local_receiver_state() -> str:
    """Describe the local receiver without mistaking a muted route for audio."""
    config = get_config()
    sinks = [
        item
        for item in pulse_objects("sinks")
        if item.get("name") == config.displayport_sink
    ]
    streams = []
    for item in pulse_objects("sink-inputs"):
        properties = item.get("properties")
        if isinstance(properties, dict) and properties.get("application.name") == (
            "Shairport Sync"
        ):
            streams.append(item)
    if len(sinks) != 1 or len(streams) != 1:
        return "route"
    if streams[0].get("sink") != sinks[0].get("index"):
        return "route"
    if streams[0].get("mute") is True:
        return "muted"
    if streams[0].get("corked") is True:
        return "corked"
    return "ready"


def local_receiver_route_current() -> bool:
    """A mute is a separate fault; the receiver is still correctly routed."""
    return local_receiver_state() != "route"


def wait_for_local_receiver_route(wait_seconds: int) -> bool:
    """Allow mDNS and OwnTone time to reconnect a restarted local receiver."""
    deadline = time.monotonic() + wait_seconds
    while True:
        if local_receiver_route_current():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


def check_flow(wait_seconds: int, *, verbose: bool = True) -> int:
    if not runtime_socket_mounts_current():
        announce(
            "Audio flow check failed: a container has a stale or unavailable "
            "PipeWire/Pulse socket mount.",
            verbose=verbose,
            error=True,
        )
        return 1
    if not configured_outputs_selected():
        announce(
            "Audio flow check skipped: the configured PC + WiiM outputs are not "
            "selected.",
            verbose=verbose,
        )
        return 0
    if not wait_for_local_receiver_route(wait_seconds):
        announce(
            "Audio flow check failed: the local Shairport receiver is missing, "
            "duplicated, or routed to the wrong PC sink.",
            verbose=verbose,
            error=True,
        )
        return 1
    if local_receiver_state() == "muted":
        announce(
            "Audio flow check failed: the local Shairport receiver is muted; "
            "PC playback cannot be verified.",
            verbose=verbose,
            error=True,
        )
        return 1
    if not soloist_has_active_playback():
        announce("Audio flow check skipped: Spotify is idle.", verbose=verbose)
        return 0
    if local_receiver_state() == "corked":
        announce(
            "Audio flow check failed: the local Shairport receiver is still "
            "corked during active playback.",
            verbose=verbose,
            error=True,
        )
        return 1

    config = get_config()
    bridge_monitor = config.bridge_sink + ".monitor"
    output_monitor = config.displayport_sink + ".monitor"
    devices = (bridge_monitor, output_monitor)

    input_peak = 0
    output_peak = 0
    for _attempt in range(wait_seconds):
        peaks = sample_peaks(devices)
        input_peak = max(input_peak, peaks[bridge_monitor])
        output_peak = max(output_peak, peaks[output_monitor])
        if input_peak >= ACTIVE_PEAK:
            break
    if input_peak < ACTIVE_PEAK:
        announce(
            "Audio flow check skipped: active Spotify playback produced no "
            "testable PCM.",
            verbose=verbose,
        )
        return 0
    if output_peak >= ACTIVE_PEAK:
        announce(
            "Audio flow check passed: live PCM reached the PC output.",
            verbose=verbose,
        )
        return 0

    # AirPlay and Shairport add buffering after source PCM becomes audible, so
    # the downstream path receives a separate complete grace period.
    for _attempt in range(wait_seconds):
        peaks = sample_peaks(devices)
        if peaks[output_monitor] >= ACTIVE_PEAK:
            announce(
                "Audio flow check passed: live PCM reached the PC output.",
                verbose=verbose,
            )
            return 0

    announce(
        "Audio flow check failed: Spotify PCM is active but the PC bridge "
        "output remained digitally silent.",
        verbose=verbose,
        error=True,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=20,
        help="seconds to wait for input, then output PCM (default: 20)",
    )
    args = parser.parse_args()
    if not 1 <= args.wait_seconds <= 60:
        parser.error("--wait-seconds must be between 1 and 60")
    try:
        return check_flow(args.wait_seconds)
    except (
        ConfigError,
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Audio flow check unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
