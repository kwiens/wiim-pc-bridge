from __future__ import annotations

import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


class ServiceStartupContractTests(unittest.TestCase):
    def test_stack_is_cold_started_before_output_reconciliation(self) -> None:
        unit = (PROJECT / "systemd/wiim-pc-bridge.service.in").read_text(
            encoding="utf-8"
        )
        runtime = unit.index("/ensure-runtime.sh")
        stop = unit.index(" compose --env-file ", runtime)
        self.assertIn(" stop --timeout 20", unit[stop : stop + 160])
        start = unit.index(" up -d --wait", stop)
        reconcile = unit.index("/bridge.py reconcile", start)
        monitor = unit.index("ExecStart=/usr/bin/python3", reconcile)
        flow_check = unit.index("ExecStartPost=/usr/bin/python3", monitor)
        self.assertLess(runtime, stop)
        self.assertLess(stop, start)
        self.assertLess(start, reconcile)
        self.assertLess(reconcile, monitor)
        self.assertLess(monitor, flow_check)

    def test_cold_stop_is_not_allowed_to_fail_silently(self) -> None:
        unit = (PROJECT / "systemd/wiim-pc-bridge.service.in").read_text(
            encoding="utf-8"
        )
        stop_line = next(
            line for line in unit.splitlines() if " stop --timeout " in line
        )
        self.assertTrue(stop_line.startswith("ExecStartPre=/usr/bin/docker "))


if __name__ == "__main__":
    unittest.main()
