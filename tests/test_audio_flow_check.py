from __future__ import annotations

import io
import unittest
from unittest import mock

import audio_flow_check as flow
from bridge_config import get_config
from tests.support import configured


class PcmPeakTests(unittest.TestCase):
    def test_silence_and_negative_full_scale(self) -> None:
        self.assertEqual(flow.pcm_peak(bytes(32)), 0)
        self.assertEqual(flow.pcm_peak(b"\x00\x80"), 32768)
        self.assertEqual(flow.pcm_peak(b"\x01"), 0)


class FlowCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = configured()
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)
        config = get_config()
        self.input = config.bridge_sink + ".monitor"
        self.output = config.displayport_sink + ".monitor"

    def test_idle_spotify_skips_capture(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=False
            ),
            mock.patch("audio_flow_check.sample_peaks") as sample,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 0)
        sample.assert_not_called()

    def test_quiet_mode_emits_no_output(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=False
            ),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            self.assertEqual(flow.check_flow(2, verbose=False), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_unverifiable_silence_does_not_restart_the_bridge(self) -> None:
        silence = {self.input: 0, self.output: 0}
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=True
            ),
            mock.patch("audio_flow_check.sample_peaks", return_value=silence) as sample,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 0)
        self.assertEqual(sample.call_count, 2)

    def test_live_pcm_reaching_output_passes(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=True
            ),
            mock.patch(
                "audio_flow_check.sample_peaks",
                return_value={self.input: 1200, self.output: 900},
            ) as sample,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 0)
        sample.assert_called_once_with((self.input, self.output))

    def test_active_input_with_silent_output_fails(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=True
            ),
            mock.patch(
                "audio_flow_check.sample_peaks",
                return_value={self.input: 1200, self.output: 0},
            ) as sample,
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(3), 1)
        self.assertEqual(sample.call_count, 4)

    def test_output_may_arrive_after_airplay_buffering(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=True
            ),
            mock.patch(
                "audio_flow_check.sample_peaks",
                side_effect=[
                    {self.input: 1000, self.output: 0},
                    {self.input: 800, self.output: 0},
                    {self.input: 700, self.output: 600},
                ],
            ) as sample,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(3), 0)
        self.assertEqual(sample.call_count, 3)


if __name__ == "__main__":
    unittest.main()
