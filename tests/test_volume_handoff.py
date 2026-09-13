from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import volume_handoff


class VolumeHandoffTests(unittest.TestCase):
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
        self.assertIsNone(tracker.observe_activity(True))
        self.assertIsNone(tracker.observe_activity(True))
        self.assertIsNone(tracker.observe_activity(False))
        self.assertEqual(tracker.observe_activity(True), 62)
        self.assertIsNone(tracker.observe_activity(True))

    def test_sudden_target_volume_does_not_replace_stable_source(self) -> None:
        tracker = volume_handoff.VolumeTracker(saved_volume=62, active=False)
        self.assertIsNone(tracker.observe_source_volume(100, now=1.0))
        self.assertEqual(tracker.observe_activity(True), 62)

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


if __name__ == "__main__":
    unittest.main()
