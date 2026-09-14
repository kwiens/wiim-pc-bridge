from __future__ import annotations

import unittest
from unittest import mock

import bridge
from tests.support import configured

LOCAL = "Test PC Output"
WIIM = "Test WiiM Group"
LEADER = "192.0.2.10"
FOLLOWER = "192.0.2.11"


def wiim_responses(
    followers: list[dict[str, object]] | None = None,
    leader_group: str = "0",
    follower_group: str = "1",
    master_ip: str = LEADER,
):
    """Build a fake WiiM API keyed on (address, command)."""
    listed = (
        followers
        if followers is not None
        else [{"name": "Living Room", "ip": FOLLOWER}]
    )

    def respond(ip: str, command: str) -> dict[str, object]:
        if command == "multiroom:getSlaveList":
            return {"slaves": len(listed), "slave_list": listed}
        if ip == LEADER:
            return {"group": leader_group}
        return {"group": follower_group, "master_ip": master_ip}

    return respond


def output(identifier: str, name: str, kind: str, **fields: object):
    item = {"id": identifier, "name": name, "type": kind, "selected": False}
    item.update(fields)
    return item


class RequestTests(unittest.TestCase):
    @mock.patch.object(bridge.urllib.request, "urlopen")
    def test_malformed_owntone_json_has_a_concise_error(
        self, urlopen: mock.Mock
    ) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{"
        urlopen.return_value = response
        with self.assertRaisesRegex(bridge.BridgeError, "malformed JSON"):
            bridge.request("/outputs")

    @mock.patch.object(bridge.urllib.request, "urlopen")
    def test_a_stall_while_reading_the_body_becomes_a_bridge_error(
        self, urlopen: mock.Mock
    ) -> None:
        # urllib only wraps connect-phase failures in URLError. A timeout during
        # read() raises a bare TimeoutError, which used to escape main()'s
        # handler as a traceback.
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = TimeoutError("timed out")
        urlopen.return_value = response
        with self.assertRaisesRegex(bridge.BridgeError, "OwnTone API request failed"):
            bridge.request("/outputs")

    @mock.patch.object(bridge.urllib.request, "urlopen")
    def test_a_truncated_body_becomes_a_bridge_error(self, urlopen: mock.Mock) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = (
            bridge.http.client.IncompleteRead(b"abc", 97)
        )
        urlopen.return_value = response
        with self.assertRaisesRegex(bridge.BridgeError, "OwnTone API request failed"):
            bridge.request("/outputs")


class GroupValidationTests(unittest.TestCase):
    def test_the_configured_group_is_accepted(self) -> None:
        with (
            configured(),
            mock.patch.object(bridge, "wiim_request", side_effect=wiim_responses()),
        ):
            bridge.validate_wiim_group()

    def test_a_standalone_leader_is_rejected(self) -> None:
        with (
            configured(),
            mock.patch.object(
                bridge, "wiim_request", side_effect=wiim_responses(leader_group="1")
            ),
            self.assertRaisesRegex(
                bridge.BridgeError, "not acting as the group leader"
            ),
        ):
            bridge.validate_wiim_group()

    def test_a_follower_with_the_wrong_identity_is_rejected(self) -> None:
        # The leader reports one follower, but not the configured one. This is
        # the check that keeps a rogue device out of the group.
        rogue = [{"name": "Someone Else", "ip": "192.0.2.99"}]
        with (
            configured(),
            mock.patch.object(
                bridge, "wiim_request", side_effect=wiim_responses(followers=rogue)
            ) as request,
            self.assertRaises(bridge.BridgeError) as caught,
        ):
            bridge.validate_wiim_group()
        message = str(caught.exception)
        self.assertIn("Living Room (192.0.2.11)", message)
        self.assertIn("Someone Else (192.0.2.99)", message)
        request.assert_called()

    def test_an_extra_unconfigured_follower_is_rejected(self) -> None:
        extra = [
            {"name": "Living Room", "ip": FOLLOWER},
            {"name": "Patio", "ip": "192.0.2.12"},
        ]
        with (
            configured(),
            mock.patch.object(
                bridge, "wiim_request", side_effect=wiim_responses(followers=extra)
            ),
            self.assertRaisesRegex(bridge.BridgeError, "unexpected: Patio"),
        ):
            bridge.validate_wiim_group()

    def test_a_follower_pointing_at_another_leader_is_rejected(self) -> None:
        with (
            configured(),
            mock.patch.object(
                bridge,
                "wiim_request",
                side_effect=wiim_responses(master_ip="192.0.2.77"),
            ),
            self.assertRaisesRegex(bridge.BridgeError, "rather than Kitchen"),
        ):
            bridge.validate_wiim_group()

    def test_an_ungrouped_follower_is_rejected(self) -> None:
        with (
            configured(),
            mock.patch.object(
                bridge, "wiim_request", side_effect=wiim_responses(follower_group="0")
            ),
            self.assertRaisesRegex(bridge.BridgeError, "not joined to a group"),
        ):
            bridge.validate_wiim_group()

    def test_two_followers_are_accepted_when_both_are_configured(self) -> None:
        listed = [
            {"name": "Living Room", "ip": FOLLOWER},
            {"name": "Patio", "ip": "192.0.2.12"},
        ]
        with (
            configured(WIIM_FOLLOWERS="192.0.2.11=Living Room,192.0.2.12=Patio"),
            mock.patch.object(
                bridge, "wiim_request", side_effect=wiim_responses(followers=listed)
            ),
        ):
            bridge.validate_wiim_group()

    def test_a_string_typed_slave_count_does_not_matter(self) -> None:
        # The group is judged by the follower set, not by a count whose JSON
        # type varies between LinkPlay firmware versions.
        def respond(ip: str, command: str) -> dict[str, object]:
            if command == "multiroom:getSlaveList":
                return {
                    "slaves": "1",
                    "slave_list": [{"name": "Living Room", "ip": FOLLOWER}],
                }
            if ip == LEADER:
                return {"group": "0"}
            return {"group": "1", "master_ip": LEADER}

        with (
            configured(),
            mock.patch.object(bridge, "wiim_request", side_effect=respond),
        ):
            bridge.validate_wiim_group()


class OutputTests(unittest.TestCase):
    def test_output_type_must_match(self) -> None:
        items = [
            output("wrong", WIIM, "AirPlay 1"),
            output("right", WIIM, "AirPlay 2"),
        ]
        with configured():
            self.assertEqual(bridge.resolve_target(items, "wiim")["id"], "right")

    def test_output_without_id_is_rejected(self) -> None:
        items = [{"name": LOCAL, "type": "AirPlay 1"}]
        with configured(), self.assertRaisesRegex(bridge.BridgeError, "missing its id"):
            bridge.resolve_target(items, "local")

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

    def test_offset_range_is_enforced(self) -> None:
        with self.assertRaisesRegex(bridge.BridgeError, "between -2000 and 2000"):
            bridge.set_output_offset({"id": "42"}, 2001)


class ReconcileTests(unittest.TestCase):
    def desired(self) -> list[dict[str, object]]:
        return [
            output(
                "local",
                LOCAL,
                "AirPlay 1",
                selected=True,
                volume=71,
                offset_ms=125,
            ),
            output(
                "wiim",
                WIIM,
                "AirPlay 2",
                selected=True,
                volume=39,
                offset_ms=-80,
            ),
        ]

    def test_reconcile_deselects_then_sets_levels_then_selects(self) -> None:
        state = self.desired()
        with (
            configured(),
            mock.patch.object(bridge, "validate_wiim_group"),
            mock.patch.object(bridge, "outputs", side_effect=[state, state]),
            mock.patch.object(bridge, "set_outputs") as select,
            mock.patch.object(bridge, "set_output_volume") as volume,
            mock.patch.object(bridge, "set_output_offset") as offset,
        ):
            recorder = mock.Mock()
            recorder.attach_mock(select, "select")
            recorder.attach_mock(volume, "volume")
            recorder.attach_mock(offset, "offset")
            bridge.reconcile_once()

        self.assertEqual(
            recorder.mock_calls,
            [
                # The deselect is what actually prevents a cached-volume burst;
                # setting levels on already-connected outputs is too late.
                mock.call.select([]),
                mock.call.volume(state[0], 71),
                mock.call.volume(state[1], 39),
                mock.call.offset(state[0], 125),
                mock.call.offset(state[1], -80),
                mock.call.select(state),
            ],
        )

    def test_swapped_levels_are_detected_by_the_verification_pass(self) -> None:
        # The second read reports what OwnTone actually kept. If the write went
        # to the wrong output, reconcile must fail rather than report success.
        applied = self.desired()
        drifted = [
            output(
                "local", LOCAL, "AirPlay 1", selected=True, volume=39, offset_ms=125
            ),
            output("wiim", WIIM, "AirPlay 2", selected=True, volume=71, offset_ms=-80),
        ]
        with (
            configured(),
            mock.patch.object(bridge, "validate_wiim_group"),
            mock.patch.object(bridge, "outputs", side_effect=[applied, drifted]),
            mock.patch.object(bridge, "set_outputs"),
            mock.patch.object(bridge, "set_output_volume"),
            mock.patch.object(bridge, "set_output_offset"),
            self.assertRaisesRegex(bridge.BridgeError, "did not retain"),
        ):
            bridge.reconcile_once()

    def test_swapped_offsets_are_detected_by_the_verification_pass(self) -> None:
        applied = self.desired()
        drifted = [
            output(
                "local", LOCAL, "AirPlay 1", selected=True, volume=71, offset_ms=-80
            ),
            output("wiim", WIIM, "AirPlay 2", selected=True, volume=39, offset_ms=125),
        ]
        with (
            configured(),
            mock.patch.object(bridge, "validate_wiim_group"),
            mock.patch.object(bridge, "outputs", side_effect=[applied, drifted]),
            mock.patch.object(bridge, "set_outputs"),
            mock.patch.object(bridge, "set_output_volume"),
            mock.patch.object(bridge, "set_output_offset"),
            self.assertRaisesRegex(bridge.BridgeError, "did not retain"),
        ):
            bridge.reconcile_once()

    def test_an_extra_selected_output_fails_verification(self) -> None:
        applied = self.desired()
        with_extra = [
            *self.desired(),
            output(
                "rogue",
                "Living Room WiiM",
                "AirPlay 2",
                selected=True,
                volume=50,
                offset_ms=0,
            ),
        ]
        with (
            configured(),
            mock.patch.object(bridge, "validate_wiim_group"),
            mock.patch.object(bridge, "outputs", side_effect=[applied, with_extra]),
            mock.patch.object(bridge, "set_outputs"),
            mock.patch.object(bridge, "set_output_volume"),
            mock.patch.object(bridge, "set_output_offset"),
            self.assertRaisesRegex(bridge.BridgeError, "did not retain"),
        ):
            bridge.reconcile_once()

    def test_select_all_fails_closed_before_touching_outputs(self) -> None:
        with (
            configured(),
            mock.patch.object(
                bridge,
                "validate_wiim_group",
                side_effect=bridge.BridgeError("bad group"),
            ),
            mock.patch.object(bridge, "outputs") as read,
            mock.patch.object(bridge, "set_outputs") as select,
            self.assertRaisesRegex(bridge.BridgeError, "bad group"),
        ):
            bridge.select_all(confirmed=True)
        read.assert_not_called()
        select.assert_not_called()

    def test_select_all_requires_the_takeover_flag(self) -> None:
        with (
            configured(),
            mock.patch.object(bridge, "validate_wiim_group") as validate,
            self.assertRaisesRegex(bridge.BridgeError, "confirm-wiim-takeover"),
        ):
            bridge.select_all(confirmed=False)
        validate.assert_not_called()


class ReconcileRetryTests(unittest.TestCase):
    def test_retrying_gives_up_at_the_deadline(self) -> None:
        clock = iter([0.0, 0.0, 1.0, 2.0, 400.0, 400.0])
        with (
            configured(),
            mock.patch.object(
                bridge,
                "reconcile_once",
                side_effect=bridge.BridgeError("not ready"),
            ),
            mock.patch.object(bridge.time, "monotonic", lambda: next(clock)),
            mock.patch.object(bridge.time, "sleep"),
            self.assertRaisesRegex(bridge.BridgeError, "did not become ready"),
        ):
            bridge.reconcile(300)

    def test_retrying_stops_as_soon_as_it_succeeds(self) -> None:
        attempts = [bridge.BridgeError("not ready"), None]

        def once() -> None:
            outcome = attempts.pop(0)
            if outcome is not None:
                raise outcome

        with (
            configured(),
            mock.patch.object(
                bridge, "reconcile_once", side_effect=once
            ) as reconcile_once,
            mock.patch.object(bridge.time, "sleep"),
            mock.patch("builtins.print"),
        ):
            bridge.reconcile(300)
        self.assertEqual(reconcile_once.call_count, 2)

    def test_an_out_of_range_wait_is_rejected(self) -> None:
        for wait in (-1, 601):
            with (
                self.subTest(wait=wait),
                self.assertRaisesRegex(bridge.BridgeError, "between 0 and 600"),
            ):
                bridge.reconcile(wait)


if __name__ == "__main__":
    unittest.main()
