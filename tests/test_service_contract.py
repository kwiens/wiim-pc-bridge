from __future__ import annotations

import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


class ServiceStartupContractTests(unittest.TestCase):
    def test_stack_lifecycle_is_separate_from_supervision(self) -> None:
        unit = (PROJECT / "systemd/wiim-pc-bridge.service.in").read_text(
            encoding="utf-8"
        )
        runtime = unit.index("/ensure-runtime.sh")
        start = unit.index(" up -d --wait", runtime)
        self.assertLess(runtime, start)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertNotIn("ExecStartPre=/usr/bin/docker", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=30s", unit)
        self.assertNotIn("ExecStopPost", unit)

    def test_child_monitor_restarts_cannot_stop_the_stack(self) -> None:
        for name in ("supervisor", "volume"):
            unit = (PROJECT / f"systemd/wiim-pc-bridge-{name}.service.in").read_text()
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("PartOf=wiim-pc-bridge.service", unit)
            self.assertNotIn("ExecStop", unit)
            self.assertNotIn("docker", unit)

    def test_capture_cannot_terminate_the_spotify_session(self) -> None:
        entrypoint = (PROJECT / "soloist-entrypoint.sh").read_text()
        self.assertNotIn("capture_pid", entrypoint)
        self.assertNotIn("parec ", entrypoint)
        capture = (PROJECT / "capture-entrypoint.sh").read_text()
        self.assertIn("capture_relay.py", capture)
        self.assertNotIn("kill", capture)
        compose = (PROJECT / "compose.yaml").read_text()
        self.assertIn("dockerfile: Dockerfile.capture", compose)


if __name__ == "__main__":
    unittest.main()
