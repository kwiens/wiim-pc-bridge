#!/usr/bin/env python3
"""Verify active Spotify PCM reaches the local bridge output."""

from __future__ import annotations

import argparse
import array
import json
import os
import selectors
import subprocess  # nosec B404
import sys
import time

from bridge_config import ConfigError, get_config

DOCKER = "/usr/bin/docker"
PAREC = "/usr/bin/parec"
SOLOIST_CONTAINER = "wiim-pc-bridge-soloist"
SOLOIST_WS = "127.0.0.1:9090"
ACTIVE_PEAK = 4
SAMPLE_SECONDS = 1


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
                data = os.read(key.fileobj.fileno(), 65536)
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


def check_flow(wait_seconds: int, *, verbose: bool = True) -> int:
    if not soloist_has_active_playback():
        announce("Audio flow check skipped: Spotify is idle.", verbose=verbose)
        return 0

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
