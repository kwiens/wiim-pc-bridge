from __future__ import annotations

import io
import unittest
from unittest import mock

import audio_flow_check as flow
import bridge
from bridge_config import get_config
from tests.support import configured


class PcmPeakTests(unittest.TestCase):
    def test_silence_and_negative_full_scale(self) -> None:
        self.assertEqual(flow.pcm_peak(bytes(32)), 0)
        self.assertEqual(flow.pcm_peak(b"\x00\x80"), 32768)
        self.assertEqual(flow.pcm_peak(b"\x01"), 0)


class OutputSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = configured()
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)
        config = get_config()
        self.local = {
            "id": "local-id",
            "name": config.local_output_name,
            "type": bridge.LOCAL_TYPE,
            "selected": True,
        }
        self.wiim = {
            "id": "wiim-id",
            "name": config.wiim_output_name,
            "type": bridge.WIIM_TYPE,
            "selected": True,
        }

    def test_exact_configured_pair_is_active(self) -> None:
        with mock.patch("bridge.outputs", return_value=[self.local, self.wiim]):
            self.assertTrue(flow.configured_outputs_selected())

    def test_partial_or_extra_selection_is_not_the_managed_route(self) -> None:
        extra = {
            "id": "extra-id",
            "name": "Other room",
            "type": bridge.LOCAL_TYPE,
            "selected": True,
        }
        for items in (
            [self.local, {**self.wiim, "selected": False}],
            [
                self.local,
                self.wiim,
                extra,
            ],
        ):
            with self.subTest(items=items), mock.patch(
                "bridge.outputs", return_value=items
            ):
                self.assertFalse(flow.configured_outputs_selected())


class RuntimeSocketTests(unittest.TestCase):
    def test_container_socket_identity_is_parsed(self) -> None:
        completed = mock.Mock(stdout="67:61\n")
        with mock.patch("audio_flow_check.subprocess.run", return_value=completed):
            self.assertEqual(
                flow.container_socket_identity("container", "/socket"), (67, 61)
            )

    def test_every_container_mount_must_match_the_host_socket(self) -> None:
        with (
            configured(),
            mock.patch(
                "audio_flow_check.socket_identity",
                side_effect=[(67, 60), (67, 61)],
            ),
            mock.patch(
                "audio_flow_check.container_socket_identity",
                side_effect=[(67, 60), (67, 61), (67, 99)],
            ),
        ):
            self.assertFalse(flow.runtime_socket_mounts_current())


class ReceiverRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = configured()
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)

    def test_exactly_one_receiver_must_target_the_configured_sink(self) -> None:
        sink = {"index": 64, "name": get_config().displayport_sink}
        receiver = {
            "sink": 64,
            "properties": {"application.name": "Shairport Sync"},
        }
        with mock.patch(
            "audio_flow_check.pulse_objects", side_effect=[[sink], [receiver]]
        ):
            self.assertTrue(flow.local_receiver_route_current())

    def test_missing_duplicate_and_misdirected_receivers_are_rejected(self) -> None:
        sink = {"index": 64, "name": get_config().displayport_sink}
        receiver = {
            "sink": 64,
            "properties": {"application.name": "Shairport Sync"},
        }
        wrong = {**receiver, "sink": 99}
        for streams in ([], [receiver, receiver], [wrong]):
            with self.subTest(streams=streams), mock.patch(
                "audio_flow_check.pulse_objects", side_effect=[[sink], streams]
            ):
                self.assertFalse(flow.local_receiver_route_current())

    def test_muted_receiver_is_routed_but_cannot_verify_pc_audio(self) -> None:
        sink = {"index": 64, "name": get_config().displayport_sink}
        receiver = {
            "sink": 64,
            "mute": True,
            "properties": {"application.name": "Shairport Sync"},
        }
        with mock.patch(
            "audio_flow_check.pulse_objects", side_effect=[[sink], [receiver]] * 2
        ):
            self.assertTrue(flow.local_receiver_route_current())
            self.assertEqual(flow.local_receiver_state(), "muted")

    def test_corked_receiver_is_routed_but_not_playing(self) -> None:
        sink = {"index": 64, "name": get_config().displayport_sink}
        receiver = {
            "sink": 64,
            "mute": False,
            "corked": True,
            "properties": {"application.name": "Shairport Sync"},
        }
        with mock.patch(
            "audio_flow_check.pulse_objects", side_effect=[[sink], [receiver]] * 2
        ):
            self.assertTrue(flow.local_receiver_route_current())
            self.assertEqual(flow.local_receiver_state(), "corked")


class FlowCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = configured()
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)
        selection = mock.patch(
            "audio_flow_check.configured_outputs_selected", return_value=True
        )
        selection.start()
        self.addCleanup(selection.stop)
        sockets = mock.patch(
            "audio_flow_check.runtime_socket_mounts_current", return_value=True
        )
        sockets.start()
        self.addCleanup(sockets.stop)
        receiver = mock.patch(
            "audio_flow_check.wait_for_local_receiver_route", return_value=True
        )
        receiver.start()
        self.addCleanup(receiver.stop)
        receiver_state = mock.patch(
            "audio_flow_check.local_receiver_state", return_value="ready"
        )
        receiver_state.start()
        self.addCleanup(receiver_state.stop)
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

    def test_stale_runtime_socket_fails_even_while_spotify_is_idle(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.runtime_socket_mounts_current", return_value=False
            ),
            mock.patch("audio_flow_check.soloist_has_active_playback") as playback,
            mock.patch("audio_flow_check.sample_peaks") as sample,
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 1)
        playback.assert_not_called()
        sample.assert_not_called()

    def test_broken_receiver_route_fails_even_while_spotify_is_idle(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.wait_for_local_receiver_route", return_value=False
            ),
            mock.patch("audio_flow_check.soloist_has_active_playback") as playback,
            mock.patch("audio_flow_check.sample_peaks") as sample,
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 1)
        playback.assert_not_called()
        sample.assert_not_called()

    def test_muted_receiver_fails_without_claiming_pcm_reaches_pc(self) -> None:
        with (
            mock.patch("audio_flow_check.local_receiver_state", return_value="muted"),
            mock.patch("audio_flow_check.sample_peaks") as sample,
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 1)
        sample.assert_not_called()

    def test_corked_receiver_is_normal_at_idle_but_fails_during_playback(self) -> None:
        with (
            mock.patch("audio_flow_check.local_receiver_state", return_value="corked"),
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=False
            ),
            mock.patch("audio_flow_check.sample_peaks") as sample,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 0)
        sample.assert_not_called()
        with (
            mock.patch("audio_flow_check.local_receiver_state", return_value="corked"),
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=True
            ),
            mock.patch("audio_flow_check.sample_peaks") as sample,
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 1)
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

    def test_intentionally_deselected_outputs_skip_capture(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=True
            ),
            mock.patch(
                "audio_flow_check.configured_outputs_selected", return_value=False
            ),
            mock.patch("audio_flow_check.sample_peaks") as sample,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(flow.check_flow(2), 0)
        sample.assert_not_called()

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
