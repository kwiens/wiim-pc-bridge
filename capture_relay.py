#!/usr/bin/env python3
"""Drain live PCM continuously and drop it when OwnTone stops reading."""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import stat
import subprocess  # nosec B404
import sys
from contextlib import suppress
from pathlib import Path

BLOCK_SIZE = 1024  # Frame-aligned and below Linux PIPE_BUF: all or nothing.
stopping = False
capture: subprocess.Popen[bytes] | None = None


def stop(_signum: int, _frame: object) -> None:
    global stopping
    stopping = True
    if capture is not None and capture.poll() is None:
        capture.terminate()


def forward_block(path: Path, block: bytes, fifo_fd: int | None) -> int | None:
    """Never let a paused/absent FIFO reader backpressure parec."""
    if fifo_fd is None:
        try:
            fifo_fd = os.open(
                path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
            )
        except OSError as exc:
            if exc.errno == errno.ENXIO:  # No OwnTone reader yet.
                return None
            raise
        with suppress(OSError):
            fcntl.fcntl(fifo_fd, fcntl.F_SETPIPE_SZ, 4096)
        # A reader may already have data; nonblocking writes still bound it.
    try:
        written = os.write(fifo_fd, block)
        if written != len(block):
            raise RuntimeError("non-atomic PCM FIFO write")
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return fifo_fd  # Full pipe: discard this live block, never queue it.
        if exc.errno == errno.EPIPE:
            os.close(fifo_fd)
            return None
        raise
    return fifo_fd


def run(sink_name: str, fifo_path: Path) -> int:
    global capture
    if not stat.S_ISFIFO(fifo_path.lstat().st_mode):
        raise RuntimeError(f"PCM path is not a FIFO: {fifo_path}")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    capture = subprocess.Popen(  # nosec B603
        [
            "/usr/bin/parec",
            "--device=" + sink_name + ".monitor",
            "--property=application.id=wiim-pc-bridge.capture",
            "--format=s16le",
            "--rate=44100",
            "--channels=2",
            "--latency-msec=100",
            "--process-time-msec=20",
            "--raw",
        ],
        stdout=subprocess.PIPE,
        bufsize=0,
    )
    fifo_fd: int | None = None
    pending = bytearray()
    try:
        if capture.stdout is None:
            raise RuntimeError("parec stdout pipe was not created")
        while not stopping:
            data = os.read(capture.stdout.fileno(), 4096)
            if not data:
                break
            pending.extend(data)
            while len(pending) >= BLOCK_SIZE:
                fifo_fd = forward_block(fifo_path, bytes(pending[:BLOCK_SIZE]), fifo_fd)
                del pending[:BLOCK_SIZE]
        return 0 if stopping else 1  # An unexpected parec exit is unhealthy.
    finally:
        if fifo_fd is not None:
            os.close(fifo_fd)
        if capture.poll() is None:
            capture.terminate()
        try:
            capture.wait(timeout=3)
        except subprocess.TimeoutExpired:
            capture.kill()
            capture.wait(timeout=3)
        if capture.stdout is not None:
            capture.stdout.close()
        capture = None


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: capture_relay.py SINK_NAME FIFO_PATH")
    raise SystemExit(run(sys.argv[1], Path(sys.argv[2])))
