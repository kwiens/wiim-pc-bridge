from __future__ import annotations

import subprocess
import unittest
from unittest import mock

import bridge
import doctor
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


class IgnoreFileTests(unittest.TestCase):
    def test_trailing_slashes_and_comments_are_normalised(self) -> None:
        entries = doctor.ignore_entries("# comment\ncache/\n.env\n\n!keep\nruntime/\n")
        self.assertEqual(entries, {"cache", ".env", "runtime"})

    def test_a_negated_rule_is_not_counted_as_present(self) -> None:
        self.assertNotIn(".env", doctor.ignore_entries("!.env\n"))


if __name__ == "__main__":
    unittest.main()
