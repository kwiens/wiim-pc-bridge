from __future__ import annotations

import unittest
from unittest import mock

import bridge


def healthy_wiim_response(ip: str, command: str) -> dict[str, object]:
    if command == "multiroom:getSlaveList":
        return {
            "slaves": 1,
            "slave_list": [
                {
                    "name": bridge.CONFIG.living_room_device_name,
                    "ip": bridge.LIVING_ROOM_IP,
                }
            ],
        }
    if ip == bridge.KITCHEN_IP:
        return {"group": "0"}
    return {"group": "1", "master_ip": bridge.KITCHEN_IP}


class BridgeGuardTests(unittest.TestCase):
    @mock.patch.object(bridge, "wiim_request", side_effect=healthy_wiim_response)
    def test_expected_native_group_is_accepted(self, _request: mock.Mock) -> None:
        bridge.validate_wiim_group()

    @mock.patch.object(
        bridge,
        "wiim_request",
        return_value={"group": "0", "slaves": 0},
    )
    def test_standalone_wiims_are_rejected(self, _request: mock.Mock) -> None:
        with self.assertRaisesRegex(bridge.BridgeError, "refusing"):
            bridge.validate_wiim_group()

    def test_output_type_must_match(self) -> None:
        items = [
            {"name": bridge.WIIM_NAME, "type": "AirPlay 1", "id": "wrong"},
            {"name": bridge.WIIM_NAME, "type": "AirPlay 2", "id": "right"},
        ]
        result = bridge.find_output(items, bridge.WIIM_NAME, "AirPlay 2")
        self.assertEqual(result["id"], "right")

    @mock.patch.object(bridge, "request")
    def test_volume_uses_the_documented_per_output_endpoint(
        self, request: mock.Mock
    ) -> None:
        bridge.set_output_volume({"id": "42"}, 87)
        request.assert_called_once_with(
            "/player/volume?volume=87&output_id=42", method="PUT"
        )

    def test_volume_range_is_enforced(self) -> None:
        with self.assertRaisesRegex(bridge.BridgeError, "between 0 and 100"):
            bridge.set_output_volume({"id": "42"}, 101)

    @mock.patch.object(bridge, "set_outputs")
    @mock.patch.object(bridge, "outputs")
    @mock.patch.object(
        bridge,
        "validate_wiim_group",
        side_effect=bridge.BridgeError("bad group"),
    )
    def test_select_all_fails_closed_before_selecting(
        self,
        _group: mock.Mock,
        outputs: mock.Mock,
        set_outputs: mock.Mock,
    ) -> None:
        with self.assertRaisesRegex(bridge.BridgeError, "bad group"):
            bridge.select_all(confirmed=True)
        outputs.assert_not_called()
        set_outputs.assert_not_called()

    @mock.patch.object(bridge, "set_output_offset")
    @mock.patch.object(bridge, "set_output_volume")
    @mock.patch.object(bridge, "set_outputs")
    @mock.patch.object(bridge, "validate_wiim_group")
    @mock.patch.object(bridge, "outputs")
    def test_reconcile_restores_and_verifies_the_full_output_state(
        self,
        outputs: mock.Mock,
        _group: mock.Mock,
        set_outputs: mock.Mock,
        set_volume: mock.Mock,
        set_offset: mock.Mock,
    ) -> None:
        desired = [
            {
                "id": "local",
                "name": bridge.LOCAL_NAME,
                "type": "AirPlay 1",
                "selected": True,
                "volume": bridge.CONFIG.local_volume,
                "offset_ms": bridge.CONFIG.local_offset_ms,
            },
            {
                "id": "wiim",
                "name": bridge.WIIM_NAME,
                "type": "AirPlay 2",
                "selected": True,
                "volume": bridge.CONFIG.wiim_volume,
                "offset_ms": bridge.CONFIG.wiim_offset_ms,
            },
        ]
        outputs.side_effect = [desired, desired]

        bridge.reconcile_once()

        set_outputs.assert_called_once_with(desired)
        self.assertEqual(set_volume.call_count, 2)
        self.assertEqual(set_offset.call_count, 2)


if __name__ == "__main__":
    unittest.main()
