from __future__ import annotations

import unittest
from unittest import mock

import doctor


def desired_outputs() -> list[dict[str, object]]:
    return [
        {
            "id": "local",
            "name": doctor.CONFIG.local_output_name,
            "type": "AirPlay 1",
            "selected": True,
            "volume": doctor.CONFIG.local_volume,
            "offset_ms": doctor.CONFIG.local_offset_ms,
        },
        {
            "id": "wiim",
            "name": doctor.CONFIG.wiim_output_name,
            "type": "AirPlay 2",
            "selected": True,
            "volume": doctor.CONFIG.wiim_volume,
            "offset_ms": doctor.CONFIG.wiim_offset_ms,
        },
    ]


class DoctorOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        doctor.failures.clear()
        doctor.warnings.clear()

    @mock.patch.object(doctor, "owntone_outputs")
    def test_extra_selected_output_is_reported(self, outputs: mock.Mock) -> None:
        extra = {
            "id": "unexpected",
            "name": "Unexpected TV",
            "type": "AirPlay 2",
            "selected": True,
            "volume": 50,
            "offset_ms": 0,
        }
        outputs.return_value = [*desired_outputs(), extra]

        with mock.patch("builtins.print"):
            doctor.check_outputs()

        self.assertTrue(
            any("differ from the desired pair" in item for item in doctor.warnings)
        )

    @mock.patch.object(doctor, "owntone_outputs")
    def test_wrong_pc_volume_is_a_failure(self, outputs: mock.Mock) -> None:
        state = desired_outputs()
        state[0]["volume"] = doctor.CONFIG.local_volume - 1
        outputs.return_value = state

        with mock.patch("builtins.print"):
            doctor.check_outputs()

        self.assertTrue(any("PC output volume" in item for item in doctor.failures))


if __name__ == "__main__":
    unittest.main()
