from __future__ import annotations

import unittest
from unittest import mock

import bridge
import output_control
from tests.support import configured


class OutputControlTests(unittest.TestCase):
    def test_manual_intent_round_trip(self) -> None:
        with configured(), output_control.locked():
            self.assertTrue(output_control.automatic_enabled())
            output_control.set_automatic(False)
            self.assertFalse(output_control.automatic_enabled())
            output_control.set_automatic(True)
            self.assertTrue(output_control.automatic_enabled())

    def test_stop_disables_automatic_reconnection(self) -> None:
        with configured(), mock.patch("bridge.set_outputs") as select:
            bridge.stop()
            self.assertFalse(output_control.automatic_enabled())
        select.assert_called_once_with([])

    def test_select_local_disables_automatic_reconnection(self) -> None:
        local = {"id": "local", "name": "Test PC Output", "type": "AirPlay 1"}
        with (
            configured(),
            mock.patch("bridge.outputs", return_value=[local]),
            mock.patch("bridge.set_outputs") as select,
        ):
            bridge.select_local()
            self.assertFalse(output_control.automatic_enabled())
        select.assert_called_once_with([local])

    def test_select_all_enables_automatic_reconnection(self) -> None:
        pair = [
            {"id": "local", "name": "Test PC Output", "type": "AirPlay 1"},
            {"id": "wiim", "name": "Test WiiM Group", "type": "AirPlay 2"},
        ]
        with (
            configured(),
            mock.patch("bridge.outputs", return_value=pair),
            mock.patch("bridge.validate_wiim_group"),
            mock.patch("bridge.set_outputs"),
        ):
            output_control.set_automatic(False)
            bridge.select_all(confirmed=True)
            self.assertTrue(output_control.automatic_enabled())

    def test_service_restart_preserves_explicit_manual_intent(self) -> None:
        with configured(), mock.patch("bridge.reconcile_once") as reconcile:
            output_control.set_automatic(False)
            bridge.reconcile(0, respect_manual=True)
            self.assertFalse(output_control.automatic_enabled())
        reconcile.assert_not_called()
