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
            self.assertIsNone(
                capture_relay.forward_block(fifo, bytes(capture_relay.BLOCK_SIZE), None)
            )

    def test_live_fifo_reader_receives_frame_aligned_pcm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "audio.pcm"
            os.mkfifo(fifo)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            writer = None
            try:
                block = b"\x01\x02\x03\x04" * (capture_relay.BLOCK_SIZE // 4)
                writer = capture_relay.forward_block(fifo, block, None)
                self.assertIsNotNone(writer)
                self.assertEqual(os.read(reader, capture_relay.BLOCK_SIZE), block)
            finally:
                if writer is not None:
                    os.close(writer)
                os.close(reader)

    def test_full_fifo_discards_block_and_keeps_capture_alive(self) -> None:
        with mock.patch(
            "capture_relay.os.write",
            side_effect=BlockingIOError(errno.EAGAIN, "FIFO full"),
        ):
            self.assertEqual(
                capture_relay.forward_block(Path("unused"), bytes(1024), 7), 7
            )

    def test_closed_reader_reopens_on_a_later_block(self) -> None:
        with mock.patch(
            "capture_relay.os.write",
            side_effect=BrokenPipeError(errno.EPIPE, "reader closed"),
        ), mock.patch("capture_relay.os.close") as close:
            self.assertIsNone(
                capture_relay.forward_block(Path("unused"), bytes(1024), 7)
            )
        close.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
