from __future__ import annotations

import contextlib
import copy
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import bridge
import bridge_supervisor as supervisor
import output_control
from tests.support import configured
from tests.test_bridge import wiim_responses


class FakeRuntime:
    def __init__(self) -> None:
        self.active = True
        self.problem = None
        self.result = supervisor.Finding("verified", "test PCM verified")
        self.route_problem = None
        self.capture_problem = None
        self.receiver_problem = None
        self.actions = []
        self.session_id = "unchanged-spotify-session"
        self.source_identity = "source-started-at-1"
        self.desktop_streams = []
        self.desktop_mutes = []
        self.desktop_in_bridge = False

    def infrastructure(self):
        return self.problem

    def playing(self):
        return self.active

    def source_route(self):
        return self.route_problem

    def capture_route(self):
        return self.capture_problem

    def receiver_route(self):
        return self.receiver_problem

    def desktop_spotify_streams(self):
        return self.desktop_streams

    def set_desktop_spotify_mute(self, index, mute):
        self.desktop_mutes.append((index, mute))
        for item in self.desktop_streams:
            if item["index"] == index:
                item["mute"] = mute
                return True
        return False

    def desktop_spotify_in_bridge(self, _index):
        return self.desktop_in_bridge

    def reconcile(self, _playing, _now, _mode):
        return False

    def probe(self, _mode):
        return self.result

    def repair(self, finding, stage):
        self.actions.append((finding, stage))
        return "targeted repair"


class SupervisorSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(configured())
        contexts.enter_context(mock.patch("bridge_supervisor.write_status"))
        self.runtime = FakeRuntime()
        self.monitor = supervisor.Supervisor(self.runtime, 0)

    def test_network_loss_never_restarts_spotify_or_transport(self):
        self.runtime.result = supervisor.Finding("wiim_unavailable", "network lost")
        for now in range(0, 300, 5):
            self.monitor.tick(now)
        self.assertEqual(self.runtime.actions, [])
        self.runtime.result = supervisor.Finding("verified", "recovered")
        self.monitor.tick(300)
        self.assertEqual(self.monitor.state, "playing")
        self.assertEqual(self.runtime.session_id, "unchanged-spotify-session")

    def test_receiver_fault_has_a_bounded_recovery_budget(self):
        self.runtime.result = supervisor.Finding("downstream_silent", "output silent")
        for now in range(0, 600, 5):
            self.monitor.tick(now)
        self.assertEqual(len(self.runtime.actions), 3)
        self.assertEqual(self.monitor.source_interruptions, 0)
        self.assertEqual(self.monitor.state, "degraded")

    def test_capture_failure_is_not_a_spotify_failure(self):
        self.runtime.problem = supervisor.Finding(
            "container_down", "capture died", ("capture",)
        )
        self.monitor.tick(0)
        self.monitor.tick(15)
        self.assertEqual(self.runtime.actions[0][0].services, ("capture",))
        self.assertEqual(self.monitor.source_interruptions, 0)

    def test_host_audio_outage_waits_instead_of_restart_storm(self):
        self.runtime.problem = supervisor.Finding("host_unavailable", "Pulse down")
        for now in range(0, 120, 5):
            self.monitor.tick(now)
        self.assertEqual(self.runtime.actions, [])

    def test_desktop_spotify_audio_is_exclusive_and_restored(self):
        self.runtime.desktop_streams = [{"index": 80, "mute": False}]
        self.monitor.tick(0)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True)])
        self.monitor.tick(5)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True)])
        self.runtime.active = False
        self.monitor.tick(10)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True), (80, False)])
        self.assertIsNone(self.monitor.muted_desktop_stream)

    def test_preexisting_desktop_mute_is_not_claimed_or_restored(self):
        self.runtime.desktop_streams = [{"index": 80, "mute": True}]
        self.monitor.tick(0)
        self.runtime.active = False
        self.monitor.tick(5)
        self.assertEqual(self.runtime.desktop_mutes, [])

    def test_mute_claim_survives_supervisor_restart(self):
        self.runtime.desktop_streams = [{"index": 80, "mute": True}]
        self.monitor.muted_desktop_stream = 80
        self.runtime.active = False
        self.monitor.tick(0)
        self.assertEqual(self.runtime.desktop_mutes, [(80, False)])

    def test_mute_claim_survives_flatpak_stream_recreation(self):
        self.runtime.desktop_streams = [{"index": 80, "mute": False}]
        self.monitor.tick(0)
        self.assertEqual(self.monitor.muted_desktop_stream, 80)
        self.runtime.desktop_streams = []
        self.runtime.active = False
        self.monitor.tick(5)
        self.assertEqual(self.monitor.muted_desktop_stream, 80)
        self.runtime.desktop_streams = [{"index": 81, "mute": True}]
        self.monitor.tick(10)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True), (81, False)])
        self.assertIsNone(self.monitor.muted_desktop_stream)

    def test_recreated_desktop_stream_stays_muted_during_bridge_playback(self):
        self.runtime.desktop_streams = [{"index": 80, "mute": False}]
        self.monitor.tick(0)
        self.runtime.desktop_streams = [{"index": 81, "mute": True}]
        self.monitor.tick(5)
        self.assertEqual(self.monitor.muted_desktop_stream, 81)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True)])
        self.runtime.active = False
        self.monitor.tick(10)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True), (81, False)])

    def test_unmuted_replacement_is_guarded_in_the_same_tick(self):
        self.runtime.desktop_streams = [{"index": 80, "mute": False}]
        self.monitor.tick(0)
        self.runtime.desktop_streams = [{"index": 81, "mute": False}]
        self.monitor.tick(5)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True), (81, True)])
        self.assertEqual(self.monitor.muted_desktop_stream, 81)

    def test_desktop_stream_in_private_sink_is_muted_while_idle(self):
        self.runtime.active = False
        self.runtime.desktop_in_bridge = True
        self.runtime.desktop_streams = [{"index": 80, "mute": False}]
        self.monitor.tick(0)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True)])
        self.runtime.desktop_in_bridge = False
        self.monitor.tick(5)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True), (80, False)])

    def test_source_route_fault_does_not_strand_owned_desktop_mute(self):
        self.runtime.desktop_streams = [{"index": 80, "mute": False}]
        self.monitor.tick(0)
        self.assertEqual(self.monitor.muted_desktop_stream, 80)
        self.runtime.active = False
        self.runtime.route_problem = supervisor.Finding(
            "source_misrouted", "Soloist stream is on the desktop sink"
        )
        self.monitor.tick(5)
        self.assertEqual(self.runtime.desktop_mutes, [(80, True), (80, False)])
        self.assertIsNone(self.monitor.muted_desktop_stream)

    def test_muted_receiver_is_reported_without_automatic_restart(self):
        self.runtime.result = supervisor.Finding(
            "receiver_muted", "Local receiver is muted"
        )
        for now in range(0, 120, 5):
            self.monitor.tick(now)
        self.assertEqual(self.monitor.state, "degraded")
        self.assertEqual(self.runtime.actions, [])

    def test_capture_drift_is_repaired_immediately_without_restarting_source(self):
        self.runtime.active = False
        self.runtime.capture_problem = supervisor.Finding(
            "capture_misrouted", "capture is recording PC speaker monitor"
        )
        self.monitor.tick(0)
        self.assertEqual(len(self.runtime.actions), 1)
        self.assertEqual(self.runtime.actions[0][0].kind, "capture_misrouted")
        self.assertEqual(self.monitor.route_repairs, 1)
        self.assertEqual(self.monitor.source_interruptions, 0)

    def test_idle_receiver_drift_is_repaired_before_spotify_resumes(self):
        self.runtime.active = False
        self.runtime.receiver_problem = supervisor.Finding(
            "receiver_misrouted", "receiver left PC sink"
        )
        self.monitor.tick(0)
        self.assertEqual(self.runtime.actions, [])
        self.monitor.tick(5)
        self.assertEqual(len(self.runtime.actions), 1)
        self.assertEqual(self.runtime.actions[0][0].kind, "receiver_misrouted")

    def test_paused_misrouted_source_is_moved_without_container_restart(self):
        self.runtime.active = False
        self.runtime.route_problem = supervisor.Finding(
            "source_misrouted", "Soloist stream is on the desktop sink"
        )
        self.monitor.tick(0)
        self.assertEqual(self.runtime.actions, [])
        self.monitor.tick(5)
        self.assertEqual(len(self.runtime.actions), 1)
        self.assertEqual(self.runtime.actions[0][0].kind, "source_misrouted")
        self.assertEqual(self.monitor.route_repairs, 1)
        self.assertEqual(self.monitor.source_interruptions, 0)

    def test_source_route_moves_have_a_separate_budget(self):
        self.runtime.active = False
        self.runtime.route_problem = supervisor.Finding(
            "source_misrouted", "Soloist stream is on the desktop sink"
        )
        self.monitor.repair_times = [0, 1, 2]
        for now in range(0, 100, 5):
            self.monitor.tick(now)
        self.assertEqual(len(self.runtime.actions), supervisor.ROUTE_REPAIR_LIMIT)
        self.assertEqual(self.monitor.route_repairs, supervisor.ROUTE_REPAIR_LIMIT)
        self.assertIn("budget exhausted", self.monitor.detail)

    def test_ambiguous_source_route_is_not_repaired(self):
        self.runtime.route_problem = supervisor.Finding(
            "unknown", "Multiple Soloist streams"
        )
        for now in range(0, 30, 5):
            self.monitor.tick(now)
        self.assertEqual(self.runtime.actions, [])

    def test_docker_source_restart_is_recorded_without_supervisor_repair(self):
        self.monitor.tick(0)
        self.runtime.source_identity = "source-started-at-2"
        self.monitor.tick(15)
        self.monitor.tick(30)
        self.assertEqual(self.monitor.source_interruptions, 1)
        self.assertEqual(self.runtime.actions, [])

    def test_pause_during_probe_cancels_failure_count(self):
        self.runtime.result = supervisor.Finding("downstream_silent", "output silent")
        self.monitor.tick(0)
        self.runtime.result = supervisor.Finding("idle", "paused during probe")
        self.monitor.tick(15)
        self.runtime.active = False
        self.monitor.tick(20)
        self.assertEqual(self.runtime.actions, [])
        self.assertEqual(self.monitor.state, "idle")

    def test_source_silence_is_unverified_not_healthy_or_restartable(self):
        self.runtime.result = supervisor.Finding("unverified", "silent source")
        for now in range(0, 120, 5):
            self.monitor.tick(now)
        self.assertEqual(self.runtime.actions, [])
        self.assertEqual(self.monitor.verified_samples, 0)
        self.assertEqual(self.monitor.state, "unverified")

    def test_stopped_intent_survives_supervisor_restart(self):
        output_control.set_mode("stopped")
        self.monitor.tick(0)
        restarted = supervisor.Supervisor(self.runtime, 10)
        restarted.tick(10)
        self.assertEqual(restarted.state, "manual")
        self.assertEqual(self.runtime.actions, [])

    def test_receiver_repair_argv_never_names_soloist(self):
        runtime = supervisor.Runtime()
        with mock.patch("bridge_supervisor.command") as command:
            runtime.repair(supervisor.Finding("downstream_silent", "silent"), 0)
            runtime.repair(supervisor.Finding("downstream_silent", "silent"), 1)
        self.assertNotIn("wiim-pc-bridge-soloist", str(command.call_args_list))
        self.assertIn("wiim-pc-bridge-capture", str(command.call_args_list))

    def test_missing_pc_sink_does_not_connect_or_restart_receiver(self):
        runtime = supervisor.Runtime()
        with mock.patch.object(
            runtime, "pc_sink_available", return_value=False
        ), mock.patch(
            "audio_flow_check.configured_outputs_selected"
        ) as selected, mock.patch("bridge.reconcile_once") as reconcile:
            self.assertFalse(runtime.reconcile(True, 0, "auto"))
            finding = runtime.probe("auto")
        self.assertEqual(finding.kind, "host_unavailable")
        selected.assert_not_called()
        reconcile.assert_not_called()

    def test_muted_receiver_is_not_reported_as_verified_pc_playback(self):
        runtime = supervisor.Runtime()
        with (
            mock.patch.object(runtime, "pc_sink_available", return_value=True),
            mock.patch("audio_flow_check.local_receiver_state", return_value="muted"),
            mock.patch("audio_flow_check.sample_peaks") as sample,
        ):
            finding = runtime.probe("auto")
        self.assertEqual(finding.kind, "receiver_muted")
        sample.assert_not_called()

    def test_corked_receiver_is_not_reported_as_verified_pc_playback(self):
        runtime = supervisor.Runtime()
        with (
            mock.patch.object(runtime, "pc_sink_available", return_value=True),
            mock.patch("audio_flow_check.local_receiver_state", return_value="corked"),
            mock.patch("audio_flow_check.sample_peaks") as sample,
        ):
            finding = runtime.probe("auto")
        self.assertEqual(finding.kind, "receiver_corked")
        sample.assert_not_called()

    def test_route_repair_moves_only_identified_soloist_stream(self):
        runtime = supervisor.Runtime()
        sinks = [{"index": 41, "name": "test_bridge_sink"}]
        inputs = [
            {
                "index": 7,
                "sink": 9,
                "properties": {
                    "application.name": "spotify",
                    "application.process.binary": "soloist",
                },
            },
            {
                "index": 8,
                "sink": 9,
                "properties": {
                    "application.name": "spotify",
                    "application.process.binary": "spotify",
                },
            },
        ]

        def pulse_objects(kind):
            return sinks if kind == "sinks" else inputs

        def move(argv, timeout=30):
            self.assertEqual(
                argv,
                [
                    "/usr/bin/pactl",
                    "move-sink-input",
                    "7",
                    "test_bridge_sink",
                ],
            )
            self.assertEqual(timeout, 5)
            inputs[0]["sink"] = 41
            return ""

        with mock.patch(
            "audio_flow_check.pulse_objects", side_effect=pulse_objects
        ), mock.patch("bridge_supervisor.command", side_effect=move) as command:
            finding = runtime.source_route()
            self.assertEqual(finding.kind, "source_misrouted")
            result = runtime.repair(finding, 0)
            self.assertIsNone(runtime.source_route())
        command.assert_called_once()
        self.assertIn("Moved Soloist", result)
        self.assertEqual(inputs[1]["sink"], 9)

    def test_route_repair_never_moves_stream_if_private_sink_is_missing(self):
        runtime = supervisor.Runtime()
        with mock.patch("audio_flow_check.pulse_objects", return_value=[]), mock.patch(
            "bridge_supervisor.command"
        ) as command:
            self.assertEqual(runtime.source_route().kind, "host_unavailable")
            result = runtime.repair(
                supervisor.Finding("source_misrouted", "stale observation"), 0
            )
        command.assert_not_called()
        self.assertIn("no stream was moved", result)

    def test_capture_repair_uses_actual_source_not_target_hint(self):
        runtime = supervisor.Runtime()
        sources = [{"index": 41, "name": "test_bridge_sink.monitor"}]
        streams = [
            {
                "index": 7,
                "source": 99,
                "properties": {
                    "application.name": "parec",
                    "application.id": "wiim-pc-bridge.capture",
                    "target.object": "test_bridge_sink",
                },
            },
            {
                "index": 8,
                "source": 99,
                "properties": {
                    "application.name": "another recorder",
                    "application.process.id": "123",
                },
            },
        ]

        def pulse_objects(kind):
            return sources if kind == "sources" else streams

        def move(argv, timeout=30):
            self.assertEqual(
                argv,
                [
                    "/usr/bin/pactl",
                    "move-source-output",
                    "7",
                    "test_bridge_sink.monitor",
                ],
            )
            self.assertEqual(timeout, 5)
            streams[0]["source"] = 41
            return ""

        with mock.patch(
            "audio_flow_check.pulse_objects", side_effect=pulse_objects
        ), mock.patch("bridge_supervisor.command", side_effect=move) as command:
            finding = runtime.capture_route()
            self.assertEqual(finding.kind, "capture_misrouted")
            self.assertIn("Moved only", runtime.repair(finding, 0))
            self.assertIsNone(runtime.capture_route())
        command.assert_called_once()
        self.assertEqual(streams[1]["source"], 99)

    def test_ambiguous_capture_is_not_moved(self):
        runtime = supervisor.Runtime()
        sources = [{"index": 41, "name": "test_bridge_sink.monitor"}]
        stream = {
            "index": 7,
            "source": 99,
            "properties": {
                "application.name": "parec",
                "application.id": "wiim-pc-bridge.capture",
            },
        }
        with mock.patch(
            "audio_flow_check.pulse_objects",
            side_effect=[sources, [stream, stream]],
        ), mock.patch("bridge_supervisor.command") as command:
            self.assertEqual(runtime.capture_route().kind, "capture_unknown")
        command.assert_not_called()

    def test_receiver_repair_moves_only_tagged_shairport_stream(self):
        runtime = supervisor.Runtime()
        sinks = [{"index": 41, "name": "alsa_output.test-card"}]
        streams = [
            {
                "index": 7,
                "sink": 99,
                "properties": {
                    "application.name": "Shairport Sync",
                    "application.process.binary": "shairport-sync",
                },
            },
            {
                "index": 8,
                "sink": 99,
                "properties": {
                    "application.name": "Game",
                    "application.process.binary": "game",
                },
            },
        ]

        def pulse_objects(kind):
            return sinks if kind == "sinks" else streams

        def move(argv, timeout=30):
            self.assertEqual(
                argv,
                ["/usr/bin/pactl", "move-sink-input", "7", "alsa_output.test-card"],
            )
            self.assertEqual(timeout, 5)
            streams[0]["sink"] = 41
            return ""

        with mock.patch(
            "audio_flow_check.pulse_objects", side_effect=pulse_objects
        ), mock.patch("bridge_supervisor.command", side_effect=move) as command:
            finding = runtime.receiver_route()
            self.assertEqual(finding.kind, "receiver_misrouted")
            self.assertIn("Moved only", runtime.repair(finding, 0))
            self.assertIsNone(runtime.receiver_route())
        command.assert_called_once()
        self.assertEqual(streams[1]["sink"], 99)

    def test_desktop_stream_identity_uses_flatpak_client_not_stream_name(self):
        runtime = supervisor.Runtime()
        snapshot = [
            {
                "id": 52,
                "type": "PipeWire:Interface:Client",
                "info": {
                    "props": {"pipewire.access.portal.app_id": "com.spotify.Client"}
                },
            },
            {
                "id": 53,
                "type": "PipeWire:Interface:Client",
                "info": {"props": {"application.name": "spotify"}},
            },
        ]
        streams = [
            {"index": 80, "mute": False, "properties": {"client.id": "52"}},
            {"index": 81, "mute": False, "properties": {"client.id": "53"}},
        ]
        with mock.patch(
            "bridge_supervisor.command", return_value=json.dumps(snapshot)
        ), mock.patch("audio_flow_check.pulse_objects", return_value=streams):
            self.assertEqual(runtime.desktop_spotify_streams(), streams[:1])

    def test_desktop_stream_moves_out_of_private_sink_only_when_pc_exists(self):
        runtime = supervisor.Runtime()
        stream = {"index": 80, "sink": 7, "mute": False}
        sinks = [
            {"index": 7, "name": "test_bridge_sink"},
            {"index": 8, "name": "alsa_output.test-card"},
        ]

        def move(argv, timeout=30):
            self.assertEqual(
                argv,
                ["/usr/bin/pactl", "move-sink-input", "80", "alsa_output.test-card"],
            )
            self.assertEqual(timeout, 5)
            stream["sink"] = 8
            return ""

        with mock.patch.object(
            runtime, "desktop_spotify_streams", return_value=[stream]
        ), mock.patch("audio_flow_check.pulse_objects", return_value=sinks), mock.patch(
            "bridge_supervisor.command", side_effect=move
        ) as command:
            self.assertFalse(runtime.desktop_spotify_in_bridge(80))
        command.assert_called_once()
        stream["sink"] = 7
        with mock.patch.object(
            runtime, "desktop_spotify_streams", return_value=[stream]
        ), mock.patch(
            "audio_flow_check.pulse_objects", return_value=sinks[:1]
        ), mock.patch("bridge_supervisor.command") as command:
            self.assertTrue(runtime.desktop_spotify_in_bridge(80))
        command.assert_not_called()

    def test_recent_budget_and_counters_survive_monitor_restart(self):
        data = {
            "updated_at": 995,
            "repairs": 8,
            "route_repairs": 2,
            "source_interruptions": 1,
            "verified_samples": 100,
            "playback_sessions": 2,
            "active_seconds": 1500,
            "was_playing": True,
            "repair_timestamps": [300, 850, 900, 980],
            "route_repair_timestamps": [900, 980],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps(data))
            with mock.patch(
                "bridge_supervisor.status_path", return_value=path
            ), mock.patch("bridge_supervisor.time.time", return_value=1000):
                restarted = supervisor.Supervisor(self.runtime, 200)
                restarted.restore(200)
        self.assertEqual(restarted.repair_times, [50, 100, 180])
        self.assertEqual(restarted.settle_until, 240)
        self.assertEqual(restarted.repairs, 8)
        self.assertEqual(restarted.route_repairs, 2)
        self.assertEqual(restarted.route_repair_times, [100, 180])
        self.assertEqual(restarted.source_interruptions, 1)
        self.assertEqual(restarted.verified_samples, 100)
        restarted.tick(205)
        self.assertEqual(restarted.playback_sessions, 2)
        self.assertEqual(restarted.active_seconds, 1505)
        self.runtime.result = supervisor.Finding("downstream_silent", "silent")
        restarted.tick(240)
        restarted.tick(255)
        self.assertEqual(self.runtime.actions, [])
        self.assertIn("budget exhausted", restarted.detail)

    def test_stale_wiim_transport_reconnect_does_not_restart_containers(self):
        runtime = supervisor.Runtime()
        with mock.patch.object(
            runtime, "playing", return_value=True
        ), mock.patch.object(runtime, "source_guard") as guard, mock.patch(
            "bridge.reconcile_once"
        ) as reconcile, mock.patch("bridge.resume_pcm_playback") as resume, mock.patch(
            "bridge_supervisor.command"
        ) as command:
            runtime.repair(supervisor.Finding("wiim_disconnected", "stale"), 0)
        guard.assert_called_once()
        reconcile.assert_called_once()
        resume.assert_called_once()
        command.assert_not_called()

    def test_manual_stop_cancels_pending_wiim_reconnection(self):
        runtime = supervisor.Runtime()
        output_control.set_mode("stopped")
        with mock.patch("bridge.reconcile_once") as reconcile:
            action = runtime.repair(supervisor.Finding("wiim_disconnected", "stale"), 0)
        reconcile.assert_not_called()
        self.assertIn("cancelled", action)

    def test_another_wiim_source_cancels_pending_reconnection(self):
        runtime = supervisor.Runtime()
        with mock.patch.object(runtime, "playing", return_value=True), mock.patch(
            "bridge.wiim_request", return_value={"status": "play", "mode": "31"}
        ), mock.patch("bridge.reconcile_once") as reconcile, self.assertRaisesRegex(
            bridge.BridgeError, "another source"
        ):
            runtime.repair(supervisor.Finding("wiim_disconnected", "stale"), 0)
        reconcile.assert_not_called()

    def test_missing_capture_is_recreated_without_touching_source(self):
        runtime = supervisor.Runtime()
        names = "\n".join(
            "wiim-pc-bridge-" + name
            for name in supervisor.SERVICES
            if name != "capture"
        )
        with mock.patch("audio_flow_check.socket_identity"), mock.patch(
            "audio_flow_check.pulse_objects"
        ), mock.patch("bridge_supervisor.command", return_value=names):
            finding = runtime.infrastructure()
        self.assertEqual(finding.kind, "container_missing")
        with mock.patch("bridge_supervisor.command") as command:
            runtime.repair(finding, 0)
        self.assertEqual(
            command.call_args.args[0],
            ["/usr/bin/docker", "compose", "up", "-d", "--no-deps", "capture"],
        )


class OwnToneHarness:
    """A real loopback HTTP boundary with mutable output/player state."""

    def __init__(self):
        self.items = [
            {
                "id": "local",
                "name": "Test PC Output",
                "type": "AirPlay 1",
                "selected": False,
                "volume": 71,
                "offset_ms": 125,
            },
            {
                "id": "wiim",
                "name": "Test WiiM Group",
                "type": "AirPlay 2",
                "selected": False,
                "volume": 39,
                "offset_ms": -80,
            },
        ]
        self.player = "pause"
        self.writes = []
        self.reject_selection = False
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == "/api/outputs":
                    data = {"outputs": copy.deepcopy(harness.items)}
                elif self.path == "/api/player":
                    data = {"state": harness.player, "item_id": 42}
                elif self.path == "/api/queue":
                    data = {
                        "items": [
                            {
                                "id": 42,
                                "data_kind": "pipe",
                                "path": "/srv/media/spotify.pcm",
                            }
                        ]
                    }
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_PUT(self):
                path = urllib.parse.urlsplit(self.path)
                payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                data = json.loads(payload) if payload else None
                harness.writes.append((self.path, data))
                if path.path == "/api/outputs/set":
                    if harness.reject_selection and data["outputs"]:
                        harness.reject_selection = False
                        self.send_error(503)
                        return
                    for item in harness.items:
                        item["selected"] = item["id"] in data["outputs"]
                elif path.path == "/api/player/volume":
                    query = urllib.parse.parse_qs(path.query)
                    for item in harness.items:
                        if item["id"] == query["output_id"][0]:
                            item["volume"] = int(query["volume"][0])
                elif path.path.startswith("/api/outputs/"):
                    for item in harness.items:
                        if item["id"] == path.path.rsplit("/", 1)[1]:
                            item["offset_ms"] = data["offset_ms"]
                elif path.path == "/api/player/play":
                    harness.player = "play"
                elif path.path == "/api/player/pause":
                    harness.player = "pause"
                else:
                    self.send_error(404)
                    return
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class HttpFailureSequenceTests(unittest.TestCase):
    def setUp(self):
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(configured())
        self.server = OwnToneHarness()
        self.addCleanup(self.server.close)
        contexts.enter_context(
            mock.patch(
                "bridge.API", f"http://127.0.0.1:{self.server.server.server_port}/api"
            )
        )
        group_reply = wiim_responses()

        def wiim_reply(ip, command):
            return (
                {"status": "stop", "mode": "1"}
                if command == "getPlayerStatus"
                else group_reply(ip, command)
            )

        contexts.enter_context(
            mock.patch("bridge.wiim_request", side_effect=wiim_reply)
        )
        self.runtime = supervisor.Runtime()
        self.runtime.outputs.idle_since = None
        contexts.enter_context(
            mock.patch.object(self.runtime, "pc_sink_available", return_value=True)
        )

    def test_network_failure_pause_idle_resume_and_manual_stop_over_real_http(self):
        self.server.reject_selection = True
        with self.assertRaises(bridge.BridgeError):
            self.runtime.reconcile(True, 0, "auto")
        self.assertTrue(self.runtime.reconcile(True, 5, "auto"))
        self.assertTrue(all(item["selected"] for item in self.server.items))
        self.assertEqual(self.server.player, "play")
        self.server.items[1]["selected"] = False
        self.server.player = "pause"
        self.assertTrue(self.runtime.reconcile(True, 10, "auto"))
        self.assertEqual(self.server.player, "play")
        self.runtime.reconcile(False, 20, "auto")
        self.runtime.reconcile(False, 81, "auto")
        self.assertFalse(any(item["selected"] for item in self.server.items))
        writes = len(self.server.writes)
        for now in range(100, 1300, 60):
            self.runtime.reconcile(False, now, "auto")
        self.assertEqual(len(self.server.writes), writes)
        self.assertTrue(self.runtime.reconcile(True, 1400, "auto"))
        bridge.stop()
        self.runtime.reconcile(True, 1500, output_control.mode())
        self.assertFalse(any(item["selected"] for item in self.server.items))
        bridge.select_all(confirmed=True)
        self.assertEqual(output_control.mode(), "auto")

    def test_other_wiim_source_is_not_taken_over(self):
        with mock.patch(
            "bridge.wiim_request", return_value={"status": "play", "mode": "31"}
        ), self.assertRaisesRegex(bridge.BridgeError, "another source"):
            self.runtime.reconcile(True, 0, "auto")
        self.assertEqual(self.server.writes, [])
