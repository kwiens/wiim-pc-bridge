from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import startup_reconcile


class StartupReconcileTests(unittest.TestCase):
    def test_idle_startup_does_not_select_outputs(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=False
            ),
            mock.patch.object(
                startup_reconcile,
                "get_config",
                return_value=SimpleNamespace(keepalive_enabled=False),
            ),
            mock.patch("bridge.reconcile") as reconcile,
        ):
            self.assertEqual(startup_reconcile.main(), 0)
        reconcile.assert_called_once_with(180, activate=False, respect_manual=True)

    def test_active_startup_selects_outputs(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=True
            ),
            mock.patch.object(
                startup_reconcile,
                "get_config",
                return_value=SimpleNamespace(keepalive_enabled=False),
            ),
            mock.patch("bridge.reconcile") as reconcile,
        ):
            self.assertEqual(startup_reconcile.main(), 0)
        reconcile.assert_called_once_with(180, activate=True, respect_manual=True)

    def test_opt_in_keepalive_retains_connected_outputs(self) -> None:
        with (
            mock.patch(
                "audio_flow_check.soloist_has_active_playback", return_value=False
            ),
            mock.patch.object(
                startup_reconcile,
                "get_config",
                return_value=SimpleNamespace(keepalive_enabled=True),
            ),
            mock.patch("bridge.reconcile") as reconcile,
        ):
            self.assertEqual(startup_reconcile.main(), 0)
        reconcile.assert_called_once_with(180, activate=True, respect_manual=True)


if __name__ == "__main__":
    unittest.main()
