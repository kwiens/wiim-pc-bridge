from __future__ import annotations

import array
import io
import math
import sys
import threading
import unittest
from unittest import mock

import bridge
import idle_keepalive as keepalive
from bridge_config import ConfigError, get_config, load_config
from tests.support import configured


class BurstTests(unittest.TestCase):
    def test_pcm_format_level_and_fades(self) -> None:
        data = keepalive.make_burst(3, -42)
        self.assertEqual(len(data), 44100 * 3 * 2 * 2)
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder != "little":
            samples.byteswap()
        self.assertEqual(samples[0:2].tolist(), [0, 0])
        self.assertEqual(samples[-2:].tolist(), [0, 0])
        self.assertEqual(samples[::2], samples[1::2])
        peak = keepalive.pcm_peak(data)
        expected = 32767 * 10 ** (-42 / 20)
        self.assertLessEqual(peak, math.ceil(expected))
        self.assertGreater(peak, expected - 2)
        self.assertLess(keepalive.pcm_peak(data[:400]), peak / 10)

    def test_silence_and_negative_full_scale(self) -> None:
        self.assertEqual(keepalive.pcm_peak(bytes(100)), 0)
        self.assertEqual(keepalive.pcm_peak(b"\x00\x80"), 32768)


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = configured()
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)
        self.config = get_config()
        self.output = {
            "id": "123",
            "name": "Test WiiM Group",
            "type": "AirPlay 2",
            "selected": True,
        }
        self.outputs = mock.patch("bridge.outputs", return_value=[self.output]).start()
        self.spotify = mock.patch(
            "idle_keepalive.spotify_playing", return_value=False
        ).start()
        self.soloist = mock.patch(
            "idle_keepalive.soloist_playing", return_value=False
        ).start()
        self.topology = mock.patch("bridge.validate_wiim_group").start()
        self.status = mock.patch(
            "bridge.wiim_request", return_value={"status": "stop", "mode": "31"}
        ).start()
        self.writes = mock.patch("bridge.set_outputs").start()
        self.addCleanup(mock.patch.stopall)

    def test_idle_native_source_is_allowed_without_writes(self) -> None:
        self.assertIsNone(keepalive.idle_guard(self.config))
        self.topology.assert_called_once_with()
        self.writes.assert_not_called()
        self.assertEqual(self.status.call_count, 2)

    def test_existing_silent_airplay_transport_is_allowed(self) -> None:
        self.status.side_effect = [
            {"status": "play", "mode": "1"},
            {"status": "play", "mode": "99"},
        ]
        self.assertIsNone(keepalive.idle_guard(self.config))

    def test_disconnected_output_never_reconnects(self) -> None:
        self.output["selected"] = False
        self.assertIn("disconnected", keepalive.idle_guard(self.config))
        self.status.assert_not_called()
        self.writes.assert_not_called()

    def test_native_playback_loading_and_unknown_states_are_blocked(self) -> None:
        for status in (
            {"status": "play", "mode": "31"},
            {"status": "loading", "mode": "1"},
            {},
        ):
            with self.subTest(status=status):
                self.status.return_value = status
                self.assertIsNotNone(keepalive.idle_guard(self.config))
        self.writes.assert_not_called()

    def test_follower_playing_another_source_is_blocked(self) -> None:
        self.status.side_effect = [
            {"status": "play", "mode": "1"},
            {"status": "play", "mode": "41"},
        ]
        self.assertIsNotNone(keepalive.idle_guard(self.config))

    def test_muted_spotify_playback_is_blocked(self) -> None:
        self.spotify.return_value = True
        self.assertIn("Spotify", keepalive.idle_guard(self.config))
        self.status.assert_not_called()

    def test_headless_soloist_playback_is_blocked(self) -> None:
        self.soloist.return_value = True
        self.assertIn("Spotify", keepalive.idle_guard(self.config))
        self.status.assert_not_called()

    def test_invalid_topology_fails_closed(self) -> None:
        self.topology.side_effect = bridge.BridgeError("wrong group")
        with self.assertRaises(bridge.BridgeError):
            keepalive.idle_guard(self.config)
        self.writes.assert_not_called()


class SchedulerTests(unittest.TestCase):
    def test_disabled_mode_starts_no_audio_process(self) -> None:
        with (
            configured(),
            mock.patch("idle_keepalive.ActivityMonitor") as monitor,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(keepalive.run(get_config(), threading.Event()), 0)
            monitor.assert_not_called()

    def test_waits_full_interval_then_resets_and_cleans_up(self) -> None:
        stop = threading.Event()
        clock = [699.0]
        monitor = mock.Mock(last_audio=100.0, error=None)
        monitor.ready = threading.Event()
        monitor.ready.set()

        def advance(_seconds: float) -> None:
            clock[0] += 1

        with (
            configured(KEEPALIVE_ENABLED="1"),
            mock.patch("idle_keepalive.time.monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(stop, "wait", side_effect=advance) as wait,
            mock.patch("idle_keepalive.ActivityMonitor", return_value=monitor),
            mock.patch("idle_keepalive.idle_guard", return_value=None),
            mock.patch(
                "idle_keepalive.play_burst", side_effect=lambda *_: stop.set()
            ) as play,
            mock.patch("idle_keepalive.make_burst", return_value=b"pcm"),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            config = get_config()
            self.assertEqual(keepalive.run(config, stop), 0)
            wait.assert_called_once()
            play.assert_called_once_with(config, b"pcm")
            self.assertEqual(monitor.last_audio, 700.0)
            monitor.close.assert_called_once()

    def test_one_off_blocked_check_never_sends_audio(self) -> None:
        monitor = mock.Mock(last_audio=0.0, error=None)
        monitor.ready = threading.Event()
        monitor.ready.set()
        with (
            configured(),
            mock.patch("idle_keepalive.time.monotonic", return_value=100.0),
            mock.patch("idle_keepalive.ActivityMonitor", return_value=monitor),
            mock.patch("idle_keepalive.idle_guard", return_value="another source"),
            mock.patch("idle_keepalive.play_burst") as play,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(
                keepalive.run(get_config(), threading.Event(), once=True), 1
            )
            play.assert_not_called()
            monitor.close.assert_called_once()

    def test_audio_starting_during_checks_cancels_burst(self) -> None:
        stop = threading.Event()
        monitor = mock.Mock(last_audio=0.0, error=None)
        monitor.ready = threading.Event()
        monitor.ready.set()

        def check(_config):
            monitor.last_audio = 100.0
            return None

        with (
            configured(KEEPALIVE_ENABLED="1"),
            mock.patch("idle_keepalive.time.monotonic", return_value=100.0),
            mock.patch("idle_keepalive.ActivityMonitor", return_value=monitor),
            mock.patch("idle_keepalive.idle_guard", side_effect=check),
            mock.patch("idle_keepalive.play_burst") as play,
            mock.patch.object(stop, "wait", side_effect=lambda *_: stop.set()),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            # A one-off test makes the already-idle setup due immediately.
            self.assertEqual(keepalive.run(get_config(), stop, once=True), 1)
            play.assert_not_called()
            monitor.close.assert_called_once()


class KeepaliveConfigTests(unittest.TestCase):
    def test_defaults_are_opt_in_and_conservative(self) -> None:
        with configured():
            config = load_config()
        self.assertFalse(config.keepalive_enabled)
        self.assertEqual(config.keepalive_interval_seconds, 600)
        self.assertEqual(config.keepalive_duration_seconds, 3)
        self.assertEqual(config.keepalive_level_db, -42)

    def test_unsafe_or_malformed_values_are_rejected(self) -> None:
        for key, value in (
            ("KEEPALIVE_ENABLED", "2"),
            ("KEEPALIVE_INTERVAL_SECONDS", "0"),
            ("KEEPALIVE_DURATION_SECONDS", "11"),
            ("KEEPALIVE_LEVEL_DB", "0"),
            ("KEEPALIVE_LEVEL_DB", "-61"),
            ("KEEPALIVE_ENABLED", "true"),
        ):
            with (
                self.subTest(key=key),
                configured(**{key: value}),
                self.assertRaisesRegex(ConfigError, key),
            ):
                load_config()
