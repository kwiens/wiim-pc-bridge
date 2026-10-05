from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import output_control
import volume_handoff
from tests.support import configured


def pair(local: bool = True, wiim: bool = True) -> list[dict[str, object]]:
    return [
        {
            "id": "local",
            "name": "Test PC Output",
            "type": "AirPlay 1",
            "selected": local,
        },
        {
            "id": "wiim",
            "name": "Test WiiM Group",
            "type": "AirPlay 2",
            "selected": wiim,
        },
    ]


class VolumeHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(configured())

    def test_mpris_volume_is_converted_to_percent(self) -> None:
        self.assertEqual(volume_handoff.parse_mpris_volume("d 0.621088"), 62)

    def test_invalid_mpris_volume_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            volume_handoff.parse_mpris_volume("d 1.5")

    def test_zero_is_a_valid_saved_volume(self) -> None:
        self.assertEqual(volume_handoff.effective_volume(0), 0)
        self.assertEqual(
            volume_handoff.effective_volume(None), volume_handoff.DEFAULT_VOLUME
        )

    def test_timestamped_trace_activity_is_parsed(self) -> None:
        line = '1234 {"type":"auth_state","is_active":true}\n'
        self.assertIs(volume_handoff.parse_trace_activity(line), True)
        self.assertIsNone(volume_handoff.parse_trace_activity("connected\n"))

    def test_only_inactive_to_active_transition_requests_handoff(self) -> None:
        tracker = volume_handoff.VolumeTracker(saved_volume=62)
        self.assertIsNone(tracker.observe_activity(True).handoff_volume)
        self.assertIsNone(tracker.observe_activity(True).handoff_volume)
        self.assertTrue(tracker.observe_activity(False).became_inactive)
        self.assertEqual(tracker.observe_activity(True).handoff_volume, 62)
        self.assertIsNone(tracker.observe_activity(True).handoff_volume)

    def test_only_active_to_inactive_edge_requests_receiver_reset(self) -> None:
        tracker = volume_handoff.VolumeTracker(saved_volume=62)
        self.assertFalse(tracker.observe_activity(False).became_inactive)
        self.assertFalse(tracker.observe_activity(False).became_inactive)
        self.assertFalse(tracker.observe_activity(True).became_inactive)
        self.assertTrue(tracker.observe_activity(False).became_inactive)
        self.assertFalse(tracker.observe_activity(False).became_inactive)

    def test_sudden_target_volume_does_not_replace_stable_source(self) -> None:
        tracker = volume_handoff.VolumeTracker(saved_volume=62, active=False)
        self.assertIsNone(tracker.observe_source_volume(100, now=1.0))
        self.assertEqual(tracker.observe_activity(True).handoff_volume, 62)

    def test_new_source_volume_must_settle_before_it_is_saved(self) -> None:
        tracker = volume_handoff.VolumeTracker(saved_volume=62, active=False)
        self.assertIsNone(tracker.observe_source_volume(47, now=1.0))
        self.assertIsNone(tracker.observe_source_volume(47, now=1.5))
        self.assertEqual(tracker.observe_source_volume(47, now=1.7), 47)

    def test_saved_volume_round_trips_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff-volume"
            volume_handoff.save_volume(57, path)
            self.assertEqual(volume_handoff.load_saved_volume(path), 57)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_flow_failures_require_two_consecutive_observations(self) -> None:
        tracker = volume_handoff.FlowFailureTracker()
        self.assertFalse(tracker.observe(True))
        self.assertFalse(tracker.observe(False))
        self.assertFalse(tracker.observe(True))
        self.assertTrue(tracker.observe(True))

    def test_success_resets_accumulated_flow_failures(self) -> None:
        tracker = volume_handoff.FlowFailureTracker(consecutive=1)
        self.assertFalse(tracker.observe(False))
        self.assertEqual(tracker.consecutive, 0)

    def test_idle_outputs_release_only_after_grace_period(self) -> None:
        controller = volume_handoff.IdleOutputController()
        with (
            mock.patch(
                "bridge.outputs",
                side_effect=[pair(), pair(False, False), pair(False, False)],
            ),
            mock.patch("bridge.set_outputs") as set_outputs,
            mock.patch("bridge.pause_pcm_playback") as pause,
        ):
            controller.observe(False, 1.0)
            controller.observe(False, 60.0)
            set_outputs.assert_not_called()
            controller.observe(False, 61.0)
            controller.observe(False, 90.0)
        set_outputs.assert_called_once_with([])
        pause.assert_called_once_with()
        self.assertTrue(controller.released)

    def test_idle_pipe_is_paused_once_before_airplay_release(self) -> None:
        controller = volume_handoff.IdleOutputController()
        with (
            mock.patch("bridge.pause_pcm_playback") as pause,
            mock.patch("bridge.set_outputs") as select,
        ):
            controller.observe(False, 0.0)
            controller.observe(False, 4.0)
            pause.assert_not_called()
            controller.observe(False, 5.0)
            controller.observe(False, 10.0)
        pause.assert_called_once_with()
        select.assert_not_called()

    def test_opt_in_keepalive_disables_idle_disconnect(self) -> None:
        controller = volume_handoff.IdleOutputController(enabled=False)
        with mock.patch("bridge.set_outputs") as set_outputs:
            controller.observe(False, 0.0)
            controller.observe(False, 120.0)
            self.assertFalse(controller.release())
        set_outputs.assert_not_called()

    def test_playback_recovers_outputs_lost_to_network_not_just_idle_release(
        self,
    ) -> None:
        controller = volume_handoff.IdleOutputController(released=False)
        with (
            mock.patch("bridge.outputs", return_value=pair(False, False)),
            mock.patch("bridge.reconcile_once") as reconcile,
            mock.patch("bridge.resume_pcm_playback", return_value=True) as resume,
        ):
            self.assertTrue(controller.observe(True, 10.0))
        reconcile.assert_called_once_with()
        resume.assert_called_once_with()
        self.assertFalse(controller.released)

    def test_manual_output_selection_is_not_replaced_on_resume(self) -> None:
        controller = volume_handoff.IdleOutputController(released=True)
        output_control.set_automatic(False)
        with (
            mock.patch("bridge.outputs", return_value=pair(True, False)),
            mock.patch("bridge.reconcile_once") as reconcile,
            mock.patch("bridge.resume_pcm_playback") as resume,
        ):
            controller.observe(True, 10.0)
        reconcile.assert_not_called()
        resume.assert_not_called()

    def test_idle_release_never_clears_manual_local_only_selection(self) -> None:
        controller = volume_handoff.IdleOutputController()
        output_control.set_automatic(False)
        with (
            mock.patch("bridge.set_outputs") as set_outputs,
        ):
            controller.observe(False, 0.0)
            controller.observe(False, 61.0)
        set_outputs.assert_not_called()
        self.assertFalse(controller.released)

    def test_release_requires_all_outputs_to_be_deselected(self) -> None:
        controller = volume_handoff.IdleOutputController()
        with (
            mock.patch("bridge.outputs", return_value=pair()),
            mock.patch("bridge.set_outputs"),
            self.assertRaisesRegex(
                volume_handoff.bridge.BridgeError, "did not release"
            ),
        ):
            controller.release()
        self.assertFalse(controller.released)

    def test_partial_pair_is_repaired_during_active_playback(self) -> None:
        controller = volume_handoff.IdleOutputController()
        with (
            mock.patch("bridge.outputs", return_value=pair(True, False)),
            mock.patch("bridge.reconcile_once") as reconcile,
            mock.patch("bridge.resume_pcm_playback", return_value=False),
        ):
            self.assertTrue(controller.observe(True, 0.0))
        reconcile.assert_called_once_with()

    def test_selected_outputs_with_paused_pcm_player_are_resumed(self) -> None:
        controller = volume_handoff.IdleOutputController()
        with (
            mock.patch("bridge.outputs", return_value=pair()),
            mock.patch("bridge.reconcile_once") as reconcile,
            mock.patch("bridge.resume_pcm_playback", return_value=True),
        ):
            self.assertTrue(controller.observe(True, 0.0))
        reconcile.assert_not_called()

    def test_manual_stop_is_not_undone_by_active_playback(self) -> None:
        output_control.set_automatic(False)
        controller = volume_handoff.IdleOutputController()
        with mock.patch("bridge.reconcile_once") as reconcile:
            self.assertFalse(controller.observe(True, 0.0))
        reconcile.assert_not_called()

    def test_unrelated_selected_output_is_not_replaced(self) -> None:
        controller = volume_handoff.IdleOutputController()
        with (
            mock.patch(
                "bridge.outputs",
                return_value=[*pair(False, False), {"id": "tv", "selected": True}],
            ),
            mock.patch("bridge.reconcile_once") as reconcile,
            mock.patch("bridge.resume_pcm_playback") as resume,
        ):
            self.assertFalse(controller.observe(True, 0.0))
        reconcile.assert_not_called()
        resume.assert_not_called()

    def test_recovery_retries_after_backoff_even_if_first_attempt_selected_one_output(
        self,
    ) -> None:
        controller = volume_handoff.IdleOutputController()
        with (
            mock.patch(
                "bridge.outputs", side_effect=[pair(False, False), pair(True, False)]
            ),
            mock.patch(
                "bridge.reconcile_once",
                side_effect=[volume_handoff.bridge.BridgeError("LAN down"), None],
            ) as reconcile,
            mock.patch("bridge.resume_pcm_playback", return_value=True),
        ):
            with self.assertRaisesRegex(volume_handoff.bridge.BridgeError, "LAN down"):
                controller.observe(True, 0.0)
            self.assertFalse(controller.observe(True, 1.0))
            self.assertTrue(controller.observe(True, 5.0))
        self.assertEqual(reconcile.call_count, 2)

    def test_no_reconnect_or_pcm_resume_while_idle_after_network_loss(self) -> None:
        controller = volume_handoff.IdleOutputController()
        with (
            mock.patch("bridge.outputs", return_value=pair(False, False)),
            mock.patch("bridge.reconcile_once") as reconcile,
            mock.patch("bridge.resume_pcm_playback") as resume,
        ):
            controller.observe(False, 0.0)
            controller.observe(False, 100.0)
        reconcile.assert_not_called()
        resume.assert_not_called()

    def test_receiver_recycle_is_skipped_when_local_output_is_not_selected(
        self,
    ) -> None:
        with (
            mock.patch("volume_handoff.capture_output_snapshot", return_value=None),
            mock.patch("volume_handoff.subprocess.run") as run,
            mock.patch(
                "audio_flow_check.wait_for_local_receiver_route"
            ) as wait_for_route,
        ):
            self.assertFalse(volume_handoff.recycle_local_receiver())
        run.assert_not_called()
        wait_for_route.assert_not_called()

    def test_receiver_recycle_restarts_only_shairport_and_waits_for_route(self) -> None:
        snapshot = volume_handoff.OutputSnapshot(("local", "wiim"), "local", 91, 7)
        with (
            mock.patch("volume_handoff.capture_output_snapshot", return_value=snapshot),
            mock.patch("volume_handoff.subprocess.run") as run,
            mock.patch("volume_handoff.time.sleep") as sleep,
            mock.patch("volume_handoff.restore_output_snapshot") as restore,
            mock.patch(
                "audio_flow_check.wait_for_local_receiver_route", return_value=True
            ) as wait_for_route,
        ):
            self.assertTrue(volume_handoff.recycle_local_receiver())
        run.assert_called_once_with(
            [
                volume_handoff.DOCKER,
                "restart",
                "--time",
                "10",
                volume_handoff.SHAIRPORT_CONTAINER,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        sleep.assert_called_once_with(volume_handoff.RECEIVER_ADVERTISEMENT_SECONDS)
        restore.assert_called_once_with(snapshot)
        wait_for_route.assert_called_once_with(
            volume_handoff.RECEIVER_RECONNECT_SECONDS
        )

    def test_missing_route_after_receiver_recycle_fails_recovery(self) -> None:
        snapshot = volume_handoff.OutputSnapshot(("local",), "local", 91, 7)
        with (
            mock.patch("volume_handoff.capture_output_snapshot", return_value=snapshot),
            mock.patch("volume_handoff.subprocess.run"),
            mock.patch("volume_handoff.time.sleep"),
            mock.patch("volume_handoff.restore_output_snapshot"),
            mock.patch(
                "audio_flow_check.wait_for_local_receiver_route", return_value=False
            ),
            self.assertRaisesRegex(RuntimeError, "did not return"),
        ):
            volume_handoff.recycle_local_receiver()

    def test_snapshot_restores_local_level_before_exact_selection(self) -> None:
        local = {
            "id": "local",
            "name": "Test PC Output",
            "type": "AirPlay 1",
            "selected": True,
            "volume": 91,
            "offset_ms": 7,
        }
        wiim = {
            "id": "wiim",
            "name": "Test WiiM Group",
            "type": "AirPlay 2",
            "selected": True,
            "volume": 39,
            "offset_ms": -80,
        }
        with configured(), mock.patch("bridge.outputs", return_value=[local, wiim]):
            snapshot = volume_handoff.capture_output_snapshot()
        self.assertEqual(
            snapshot,
            volume_handoff.OutputSnapshot(("local", "wiim"), "local", 91, 7),
        )

        rediscovered_local = {**local, "selected": False, "volume": 50}
        with (
            mock.patch(
                "bridge.outputs",
                side_effect=[
                    [rediscovered_local, wiim],
                    [{**rediscovered_local, "selected": True, "volume": 91}, wiim],
                ],
            ),
            mock.patch("bridge.set_output_volume") as set_volume,
            mock.patch("bridge.set_output_offset") as set_offset,
            mock.patch("bridge.set_outputs") as set_outputs,
        ):
            volume_handoff.restore_output_snapshot(snapshot)
        self.assertEqual(
            set_volume.call_args_list, [mock.call(rediscovered_local, 91)] * 2
        )
        self.assertEqual(
            set_offset.call_args_list, [mock.call(rediscovered_local, 7)] * 2
        )
        set_outputs.assert_called_once_with([rediscovered_local, wiim])


if __name__ == "__main__":
    unittest.main()
