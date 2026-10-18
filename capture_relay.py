#!/usr/bin/env python3
"""Drain live PCM without allowing a paused OwnTone FIFO to stall capture."""

from __future__ import annotations

import errno
import os
import selectors
import signal
import stat
import subprocess  # nosec B404
import sys
import time
from collections import deque
from pathlib import Path

BLOCK_SIZE = 1024  # Frame-aligned and below Linux PIPE_BUF: all or nothing.
# OwnTone may wait for its 2.25-second startup buffer. Kernel FIFO growth is
# often denied to unprivileged containers, so keep a bounded userspace buffer.
MAX_BUFFER_BYTES = 524288
stopping = False
capture: subprocess.Popen[bytes] | None = None


def stop(_signum: int, _frame: object) -> None:
    global stopping
    stopping = True
    if capture is not None and capture.poll() is None:
        capture.terminate()


class PCMBuffer:
    def __init__(self) -> None:
        self.blocks: deque[bytes] = deque()
        self.bytes = 0
        self.overruns = 0

    def clear(self) -> None:
        self.blocks.clear()
        self.bytes = 0

    def append(self, block: bytes) -> None:
        if len(block) != BLOCK_SIZE:
            raise ValueError("PCM block must be frame-aligned and atomic")
        if self.bytes + len(block) > MAX_BUFFER_BYTES:
            # A suspended reader must not replay seconds of stale sound. Keep
            # only the newest live PCM after the bounded buffer is exhausted.
            self.clear()
            self.overruns += 1
        self.blocks.append(block)
        self.bytes += len(block)

    def pop(self) -> None:
        block = self.blocks.popleft()
        self.bytes -= len(block)


def flush_fifo(path: Path, pcm: PCMBuffer, fifo_fd: int | None) -> int | None:
    """Write available blocks without ever blocking the capture reader."""
    if not pcm.blocks:
        return fifo_fd
    if fifo_fd is None:
        try:
            fifo_fd = os.open(
                path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
            )
        except OSError as exc:
            if exc.errno == errno.ENXIO:  # No OwnTone reader yet.
                pcm.clear()
                return None
            raise
    while pcm.blocks:
        try:
            written = os.write(fifo_fd, pcm.blocks[0])
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return fifo_fd
            if exc.errno == errno.EPIPE:
                os.close(fifo_fd)
                pcm.clear()
                return None
            raise
        if written != BLOCK_SIZE:
            raise RuntimeError("non-atomic PCM FIFO write")
        pcm.pop()
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
    pcm = PCMBuffer()
    pending = bytearray()
    reported_overruns = 0
    last_overrun_report = 0.0
    try:
        if capture.stdout is None:
            raise RuntimeError("parec stdout pipe was not created")
        os.set_blocking(capture.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(capture.stdout, selectors.EVENT_READ)
            writer_registered = False
            while not stopping:
                if fifo_fd is not None and pcm.blocks and not writer_registered:
                    selector.register(fifo_fd, selectors.EVENT_WRITE)
                    writer_registered = True
                elif writer_registered and (fifo_fd is None or not pcm.blocks):
                    selector.unregister(fifo_fd)
                    writer_registered = False
                for key, _mask in selector.select(timeout=1):
                    if key.fileobj == capture.stdout:
                        try:
                            data = os.read(capture.stdout.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not data:
                            return 0 if stopping else 1
                        pending.extend(data)
                        while len(pending) >= BLOCK_SIZE:
                            pcm.append(bytes(pending[:BLOCK_SIZE]))
                            del pending[:BLOCK_SIZE]
                    if writer_registered:
                        selector.unregister(fifo_fd)
                        writer_registered = False
                    fifo_fd = flush_fifo(fifo_path, pcm, fifo_fd)
                    now = time.monotonic()
                    if (
                        pcm.overruns > reported_overruns
                        and now - last_overrun_report >= 30
                    ):
                        print(
                            "PCM FIFO reader stalled; discarded buffered audio "
                            f"({pcm.overruns - reported_overruns} overrun(s))",
                            file=sys.stderr,
                            flush=True,
                        )
                        reported_overruns = pcm.overruns
                        last_overrun_report = now
        return 0
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
