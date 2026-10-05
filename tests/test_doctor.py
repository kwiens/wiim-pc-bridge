from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audio_flow_check
import bridge
import doctor
import output_control
from tests.support import configured

LOCAL = "Test PC Output"
WIIM = "Test WiiM Group"


def desired_outputs() -> list[dict[str, object]]:
    """Literal expectations, not values read back from the configuration.

    Building a fixture from CONFIG makes every assertion a tautology: the code
    under test then compares the configuration with itself.
    """
    return [
        {
            "id": "local",
            "name": LOCAL,
            "type": "AirPlay 1",
            "selected": True,
            "volume": 71,
            "offset_ms": 125,
        },
        {
            "id": "wiim",
            "name": WIIM,
            "type": "AirPlay 2",
            "selected": True,
            "volume": 39,
            "offset_ms": -80,
        },
    ]


class DoctorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        doctor.failures.clear()
        doctor.warnings.clear()
        self.addCleanup(doctor.failures.clear)
        self.addCleanup(doctor.warnings.clear)
        self.printed = mock.patch("builtins.print").start()
        self.addCleanup(mock.patch.stopall)


class OutputAuditTests(DoctorTestCase):
    def test_explicit_manual_stop_is_not_an_output_failure(self) -> None:
        state = [{**item, "selected": False} for item in desired_outputs()]
        with configured(), mock.patch.object(
            doctor, "owntone_outputs", return_value=state
        ):
            output_control.set_automatic(False)
            doctor.check_outputs()
        self.assertEqual(doctor.failures, [])

    def test_idle_outputs_may_be_disconnected(self) -> None:
        state = [{**item, "selected": False} for item in desired_outputs()]
        with (
            configured(),
            mock.patch.object(doctor, "owntone_outputs", return_value=state),
            mock.patch.object(
                audio_flow_check, "soloist_has_active_playback", return_value=False
            ),
        ):
            doctor.check_outputs()
        self.assertEqual(doctor.failures, [])

    def test_disconnected_outputs_during_playback_are_a_failure(self) -> None:
        state = [{**item, "selected": False} for item in desired_outputs()]
        with (
            configured(),
            mock.patch.object(doctor, "owntone_outputs", return_value=state),
            mock.patch.object(
                audio_flow_check, "soloist_has_active_playback", return_value=True
            ),
        ):
            doctor.check_outputs()
        self.assertIn(
            "bridge outputs are disconnected when they should be selected",
            doctor.failures,
        )

    def test_a_healthy_pair_produces_no_failures_or_warnings(self) -> None:
        with (
            configured(),
            mock.patch.object(
                doctor, "owntone_outputs", return_value=desired_outputs()
            ),
        ):
            doctor.check_outputs()
        # Exact comparison: a spurious extra report must fail this test.
        self.assertEqual(doctor.failures, [])
        self.assertEqual(doctor.warnings, [])

    def test_an_independently_selected_follower_is_a_failure(self) -> None:
        # OwnTone discovers followers under their own mDNS names, so this must
        # not depend on matching a configured device name.
        rogue = {
            "id": "rogue",
            "name": "Living Room WiiM",
            "type": "AirPlay 2",
            "selected": True,
            "volume": 50,
            "offset_ms": 0,
        }
        with (
            configured(),
            mock.patch.object(
                doctor, "owntone_outputs", return_value=[*desired_outputs(), rogue]
            ),
        ):
            doctor.check_outputs()
        self.assertEqual(
            doctor.failures,
            ["selected OwnTone outputs differ from the desired pair: Living Room WiiM"],
        )
        self.assertEqual(doctor.warnings, [])

    def test_an_unselected_extra_output_is_fine(self) -> None:
        idle = {
            "id": "other",
            "name": "Bedroom",
            "type": "AirPlay 2",
            "selected": False,
            "volume": 20,
            "offset_ms": 0,
        }
        with (
            configured(),
            mock.patch.object(
                doctor, "owntone_outputs", return_value=[*desired_outputs(), idle]
            ),
        ):
            doctor.check_outputs()
        self.assertEqual(doctor.failures, [])

    def test_malformed_selected_state_does_not_count_as_selected(self) -> None:
        state = desired_outputs()
        state[0]["selected"] = "true"
        with (
            configured(),
            mock.patch.object(doctor, "owntone_outputs", return_value=state),
        ):
            doctor.check_outputs()
        self.assertEqual(
            doctor.failures,
            [
                "selected OwnTone outputs differ from the desired pair: the pair is incomplete"
            ],
        )

    def test_a_wrong_volume_is_reported_with_both_values(self) -> None:
        state = desired_outputs()
        state[0]["volume"] = 55
        with (
            configured(),
            mock.patch.object(doctor, "owntone_outputs", return_value=state),
        ):
            doctor.check_outputs()
        self.assertEqual(doctor.failures, [f"{LOCAL} volume is 55%, expected 71%"])

    def test_a_wrong_offset_is_reported_with_both_values(self) -> None:
        state = desired_outputs()
        state[1]["offset_ms"] = 0
        with (
            configured(),
            mock.patch.object(doctor, "owntone_outputs", return_value=state),
        ):
            doctor.check_outputs()
        self.assertEqual(doctor.failures, [f"{WIIM} offset is 0 ms, expected -80 ms"])

    def test_a_missing_output_is_reported_once(self) -> None:
        with (
            configured(),
            mock.patch.object(
                doctor, "owntone_outputs", return_value=desired_outputs()[:1]
            ),
        ):
            doctor.check_outputs()
        self.assertEqual(len(doctor.failures), 1)
        self.assertIn("Expected exactly one AirPlay 2 output", doctor.failures[0])


class TopologyAuditTests(DoctorTestCase):
    def test_a_topology_failure_carries_the_bridge_explanation(self) -> None:
        with (
            configured(),
            mock.patch.object(
                bridge,
                "validate_wiim_group",
                side_effect=bridge.BridgeError("missing: Patio (192.0.2.12)"),
            ),
        ):
            doctor.check_topology()
        self.assertEqual(doctor.failures, ["missing: Patio (192.0.2.12)"])

    def test_a_healthy_topology_passes(self) -> None:
        with configured(), mock.patch.object(bridge, "validate_wiim_group"):
            doctor.check_topology()
        self.assertEqual(doctor.failures, [])


class ContainerAuditTests(DoctorTestCase):
    def test_a_container_without_a_health_check_is_not_called_healthy(self) -> None:
        with mock.patch.object(doctor, "run", return_value="running none"):
            doctor.check_containers()
        self.assertEqual(doctor.failures, [])
        self.assertEqual(
            doctor.warnings,
            [
                f"{name} is running but declares no health check"
                for name in doctor.CONTAINERS
            ],
        )

    def test_an_unhealthy_container_fails(self) -> None:
        with mock.patch.object(doctor, "run", return_value="running unhealthy"):
            doctor.check_containers()
        self.assertEqual(len(doctor.failures), len(doctor.CONTAINERS))

    def test_a_healthy_container_passes(self) -> None:
        with mock.patch.object(doctor, "run", return_value="running healthy"):
            doctor.check_containers()
        self.assertEqual(doctor.failures, [])
        self.assertEqual(doctor.warnings, [])


class SecretAuditTests(DoctorTestCase):
    def prepare_files(self, root: Path) -> tuple[Path, Path, Path]:
        project = root / "project"
        project.mkdir()
        (project / ".env").write_text("private configuration\n", encoding="utf-8")
        (project / ".env").chmod(0o600)
        (project / ".gitignore").write_text(
            ".env\ncache/\nruntime/\n", encoding="utf-8"
        )
        (project / ".dockerignore").write_text(
            ".git\n.env\ncache\nruntime\n", encoding="utf-8"
        )
        key = root / "soloist-key"
        cookie = root / "pulse-cookie"
        for credential in (key, cookie):
            credential.write_text("not-a-real-secret\n", encoding="ascii")
            credential.chmod(0o600)
        return project, key, cookie

    def test_both_credentials_and_local_config_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, key, cookie = self.prepare_files(Path(directory))
            with (
                configured(SOLOIST_KEY_FILE=str(key), PULSE_COOKIE_PATH=str(cookie)),
                mock.patch.object(doctor, "PROJECT", project),
            ):
                doctor.check_secrets()
        self.assertEqual(doctor.failures, [])

    def test_public_pulse_cookie_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, key, cookie = self.prepare_files(Path(directory))
            cookie.chmod(0o644)
            with (
                configured(SOLOIST_KEY_FILE=str(key), PULSE_COOKIE_PATH=str(cookie)),
                mock.patch.object(doctor, "PROJECT", project),
            ):
                doctor.check_secrets()
        self.assertEqual(len(doctor.failures), 1)
        self.assertIn("Pulse cookie mode is 644", doctor.failures[0])


class StartupAuditTests(DoctorTestCase):
    def test_an_inactive_service_is_reported_not_raised(self) -> None:
        # `systemctl is-active` exits non-zero for "inactive"; treating that as
        # a subprocess error hid the real state and skipped the checks below it.
        answers = {
            "loginctl": "no",
            "is-enabled": "disabled",
            "is-active": "inactive",
            "show": "0",
        }

        def fake_probe(*args: str, **_kwargs: object) -> str:
            for token, answer in answers.items():
                if token in args:
                    return answer
            return "unknown"

        with (
            mock.patch.object(doctor, "probe", side_effect=fake_probe),
            mock.patch.object(doctor, "run", return_value="unless-stopped"),
        ):
            doctor.check_startup()
        self.assertEqual(len(doctor.failures), 1)
        self.assertIn("linger=no", doctor.failures[0])
        self.assertIn("enabled=disabled", doctor.failures[0])
        self.assertIn("active=inactive", doctor.failures[0])

    def test_a_restart_policy_problem_is_still_reported(self) -> None:
        # This check used to be unreachable whenever anything above it failed.
        def fake_probe(*args: str, **_kwargs: object) -> str:
            if "loginctl" in args:
                return "no"
            if "is-enabled" in args:
                return "disabled"
            if "is-active" in args:
                return "inactive"
            return "0"

        with (
            mock.patch.object(doctor, "probe", side_effect=fake_probe),
            mock.patch.object(doctor, "run", return_value="no"),
        ):
            doctor.check_startup()
        self.assertTrue(
            any(
                "restart policy is not unless-stopped" in item
                for item in doctor.failures
            ),
            doctor.failures,
        )

    def test_an_uninstalled_service_is_a_warning_not_a_failure(self) -> None:
        # The boot service is an optional install step; a bridge that is
        # playing correctly must not exit non-zero because of it.
        def fake_probe(*args: str, **_kwargs: object) -> str:
            if "is-enabled" in args:
                return "not-found"
            return "no"

        with (
            mock.patch.object(doctor, "probe", side_effect=fake_probe),
            mock.patch.object(doctor, "run", return_value="unless-stopped"),
        ):
            doctor.check_startup()
        self.assertEqual(doctor.failures, [])
        self.assertEqual(len(doctor.warnings), 1)


class ProbeTests(unittest.TestCase):
    def test_a_non_zero_exit_is_returned_as_the_answer(self) -> None:
        # `systemctl is-active` exits 3 for "inactive". If probe() raised here,
        # every unhealthy state would surface as a subprocess error instead.
        self.assertEqual(doctor.probe("sh", "-c", "echo inactive; exit 3"), "inactive")

    def test_a_missing_command_does_not_raise(self) -> None:
        self.assertIn("unavailable", doctor.probe("definitely-not-a-command-xyz"))

    def test_run_still_raises_for_genuine_errors(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            doctor.run("sh", "-c", "exit 1")


class SinkMatchingTests(unittest.TestCase):
    def test_a_sink_index_is_matched_exactly_not_by_prefix(self) -> None:
        # Sink 550 must not satisfy a check for sink 55.
        block = 'Sink Input #7\n\tSink: 550\n\tapplication.name = "soloist"'
        self.assertEqual(doctor.block_sink_id(block), "550")
        self.assertNotEqual(doctor.block_sink_id(block), "55")

    def test_a_block_without_a_sink_field_returns_none(self) -> None:
        self.assertIsNone(doctor.block_sink_id("Sink Input #7\n\tMute: no"))

    def test_actual_capture_source_is_matched_not_target_hint(self) -> None:
        block = 'Source Output #7\n\tSource: 330807\n\ttarget.object = "wiim_bridge"'
        self.assertEqual(doctor.block_source_id(block), "330807")
        self.assertNotEqual(doctor.block_source_id(block), "52")

    def test_mute_field_is_matched_exactly(self) -> None:
        self.assertTrue(doctor.block_muted("Sink Input #7\n\tMute: yes"))
        self.assertFalse(doctor.block_muted("Sink Input #7\n\tMute: no"))
        self.assertFalse(doctor.block_muted("NotMute: yes"))

    def test_corked_field_is_matched_exactly(self) -> None:
        self.assertTrue(doctor.block_corked("Sink Input #7\n\tCorked: yes"))
        self.assertFalse(doctor.block_corked("Sink Input #7\n\tCorked: no"))
        self.assertFalse(doctor.block_corked("NotCorked: yes"))

    def test_properties_are_matched_exactly(self) -> None:
        block = (
            'application.process.id = "142"\n'
            'application.process.id.extra = "42"\n'
            'target.object = "wiim_bridge"'
        )
        self.assertEqual(doctor.block_property(block, "application.process.id"), "142")
        self.assertEqual(doctor.block_property(block, "target.object"), "wiim_bridge")
        self.assertIsNone(doctor.block_property(block, "missing"))


class DesktopAudioAuditTests(DoctorTestCase):
    def test_idle_muted_desktop_spotify_is_visible(self) -> None:
        with (
            configured(),
            mock.patch.object(
                audio_flow_check, "soloist_has_active_playback", return_value=False
            ),
            mock.patch.object(
                doctor.bridge_supervisor.Runtime,
                "desktop_spotify_streams",
                return_value=[{"index": 80, "mute": True}],
            ),
        ):
            doctor.check_desktop_spotify_audio()
        self.assertEqual(doctor.failures, [])
        self.assertEqual(len(doctor.warnings), 1)
        self.assertIn("desktop Spotify is muted", doctor.warnings[0])

    def test_bridge_owned_desktop_mute_is_not_flagged_during_playback(self) -> None:
        with (
            mock.patch.object(
                audio_flow_check, "soloist_has_active_playback", return_value=True
            ),
            mock.patch.object(
                doctor.bridge_supervisor.Runtime, "desktop_spotify_streams"
            ) as streams,
        ):
            doctor.check_desktop_spotify_audio()
        streams.assert_not_called()
        self.assertEqual(doctor.warnings, [])


class CaptureRouteAuditTests(DoctorTestCase):
    def test_requested_bridge_target_does_not_hide_actual_pc_capture(self) -> None:
        with configured():
            config = doctor.get_config()
            source_output = (
                "Source Output #7\n"
                "\tSource: 99\n"
                "\tSample Specification: s16le 2ch 44100Hz\n"
                '\tapplication.name = "parec"\n'
                '\tapplication.id = "wiim-pc-bridge.capture"\n'
                f'\ttarget.object = "{config.bridge_sink}"\n'
                '\tnode.latency = "4410/44100"\n'
                '\tpulse.attr.fragsize = "17640"'
            )

            def fake_run(*args: str) -> str:
                if args == ("pactl", "get-default-sink"):
                    return config.displayport_sink
                if args == ("pactl", "list", "short", "sinks"):
                    return f"41 {config.displayport_sink}\n42 {config.bridge_sink}"
                if args == ("pactl", "list", "short", "sources"):
                    return f"52 {config.bridge_sink}.monitor\n99 pc.monitor"
                if args[:3] == ("docker", "exec", "wiim-pc-bridge-capture"):
                    return "python3"
                if args == ("pactl", "list", "source-outputs"):
                    return source_output
                if args == ("pactl", "list", "sink-inputs"):
                    return ""
                raise AssertionError(args)

            with (
                mock.patch.object(doctor, "run", side_effect=fake_run),
                mock.patch.object(
                    audio_flow_check, "runtime_socket_mounts_current", return_value=True
                ),
            ):
                doctor.check_audio_routes()
        self.assertTrue(
            any("PCM capture is not routed" in item for item in doctor.failures),
            doctor.failures,
        )


class PublishedPathTests(unittest.TestCase):
    def test_a_path_outside_the_repository_is_never_published(self) -> None:
        # The key lives in ~/.config by design, and `git check-ignore` calls
        # anything outside the working tree "not ignored" — which must not be
        # read as "a clone would carry it".
        outside = Path.home() / ".config/wiim-pc-bridge/soloist_api_key"
        self.assertFalse(doctor.is_published(outside))
        self.assertFalse(doctor.is_published(Path("/etc/hostname")))

    def test_an_unignored_path_inside_the_repository_is_published(self) -> None:
        self.assertTrue(doctor.is_published(doctor.PROJECT / "README.md"))

    def test_an_ignored_path_inside_the_repository_is_not_published(self) -> None:
        self.assertFalse(doctor.is_published(doctor.PROJECT / "cache/owntone"))


class IgnoreFileTests(unittest.TestCase):
    def test_trailing_slashes_and_comments_are_normalised(self) -> None:
        entries = doctor.ignore_entries("# comment\ncache/\n.env\n\n!keep\nruntime/\n")
        self.assertEqual(entries, {"cache", ".env", "runtime"})

    def test_a_negated_rule_is_not_counted_as_present(self) -> None:
        self.assertNotIn(".env", doctor.ignore_entries("!.env\n"))


if __name__ == "__main__":
    unittest.main()
