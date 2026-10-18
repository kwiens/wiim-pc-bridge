from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import capture_relay


class CaptureRelayTests(unittest.TestCase):
    def test_no_fifo_reader_drops_pcm_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "audio.pcm"
            os.mkfifo(fifo)
            pcm = capture_relay.PCMBuffer()
            pcm.append(bytes(capture_relay.BLOCK_SIZE))
            self.assertIsNone(capture_relay.flush_fifo(fifo, pcm, None))
            self.assertEqual(pcm.bytes, 0)

    def test_live_fifo_reader_receives_frame_aligned_pcm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "audio.pcm"
            os.mkfifo(fifo)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            writer = None
            try:
                block = b"\x01\x02\x03\x04" * (capture_relay.BLOCK_SIZE // 4)
                pcm = capture_relay.PCMBuffer()
                pcm.append(block)
                writer = capture_relay.flush_fifo(fifo, pcm, None)
                self.assertIsNotNone(writer)
                self.assertEqual(pcm.bytes, 0)
                self.assertEqual(os.read(reader, capture_relay.BLOCK_SIZE), block)
            finally:
                if writer is not None:
                    os.close(writer)
                os.close(reader)

    def test_full_fifo_discards_block_and_keeps_capture_alive(self) -> None:
        pcm = capture_relay.PCMBuffer()
        pcm.append(bytes(capture_relay.BLOCK_SIZE))
        with mock.patch(
            "capture_relay.os.write",
            side_effect=BlockingIOError(errno.EAGAIN, "FIFO full"),
        ):
            self.assertEqual(capture_relay.flush_fifo(Path("unused"), pcm, 7), 7)
            self.assertEqual(pcm.bytes, capture_relay.BLOCK_SIZE)

    def test_stalled_reader_discards_stale_backlog_at_bound(self) -> None:
        pcm = capture_relay.PCMBuffer()
        old = bytes(capture_relay.BLOCK_SIZE)
        newest = bytes([1]) * capture_relay.BLOCK_SIZE
        for _ in range(capture_relay.MAX_BUFFER_BYTES // capture_relay.BLOCK_SIZE):
            pcm.append(old)
        pcm.append(newest)
        self.assertEqual(pcm.overruns, 1)
        self.assertEqual(list(pcm.blocks), [newest])
        self.assertEqual(pcm.bytes, capture_relay.BLOCK_SIZE)

    def test_backlogged_pcm_drains_after_fifo_reader_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "audio.pcm"
            os.mkfifo(fifo)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            pcm = capture_relay.PCMBuffer()
            try:
                blocks = [
                    bytes([index]) * capture_relay.BLOCK_SIZE for index in range(32)
                ]
                for block in blocks:
                    pcm.append(block)
                self.assertEqual(capture_relay.flush_fifo(fifo, pcm, writer), writer)
                self.assertGreater(pcm.bytes, 0)
                received = bytearray()
                while pcm.blocks:
                    received.extend(os.read(reader, 8192))
                    capture_relay.flush_fifo(fifo, pcm, writer)
                while True:
                    try:
                        piece = os.read(reader, 8192)
                    except BlockingIOError:
                        break
                    if not piece:
                        break
                    received.extend(piece)
                self.assertEqual(received, b"".join(blocks))
            finally:
                os.close(writer)
                os.close(reader)

    def test_closed_reader_reopens_on_a_later_block(self) -> None:
        pcm = capture_relay.PCMBuffer()
        pcm.append(bytes(capture_relay.BLOCK_SIZE))
        with mock.patch(
            "capture_relay.os.write",
            side_effect=BrokenPipeError(errno.EPIPE, "reader closed"),
        ), mock.patch("capture_relay.os.close") as close:
            self.assertIsNone(capture_relay.flush_fifo(Path("unused"), pcm, 7))
        self.assertEqual(pcm.bytes, 0)
        close.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
