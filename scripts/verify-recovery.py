#!/usr/bin/env python3
"""Controlled live fault injection; never changes host networking or reboots."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import audio_flow_check as flow  # noqa: E402
import bridge  # noqa: E402
import output_control  # noqa: E402


def run(argv: list[str]) -> str:
    return subprocess.run(  # nosec B603
        argv, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def source_identity() -> str:
    return run(
        [
            "/usr/bin/docker",
            "inspect",
            "--format",
            "{{.State.StartedAt}} {{.State.Pid}}",
            "wiim-pc-bridge-soloist",
        ]
    )


def ready() -> bool:
    for service in ("soloist", "capture", "owntone", "shairport"):
        state = run(
            [
                "/usr/bin/docker",
                "inspect",
                "--format",
                "{{.State.Running}} {{.State.Health.Status}}",
                "wiim-pc-bridge-" + service,
            ]
        )
        if state != "true healthy":
            return False
    for service in ("supervisor", "volume"):
        if (
            run(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "is-active",
                    "wiim-pc-bridge-" + service + ".service",
                ]
            )
            != "active"
        ):
            return False
    return True


def inject(fault: str) -> None:
    if fault in ("capture", "owntone", "shairport"):
        run(["/usr/bin/docker", "stop", "--time", "1", "wiim-pc-bridge-" + fault])
    elif fault in ("supervisor", "volume"):
        run(
            [
                "/usr/bin/systemctl",
                "--user",
                "kill",
                "--kill-whom=main",
                "--signal=SIGKILL",
                "wiim-pc-bridge-" + fault + ".service",
            ]
        )
    elif fault == "outputs":
        with output_control.locked():
            if (
                not output_control.automatic_enabled()
                or not flow.soloist_has_active_playback()
            ):
                raise RuntimeError(
                    "output fault injection requires active managed Spotify playback"
                )
            bridge.set_outputs([])
    elif fault == "player":
        with output_control.locked():
            if (
                not output_control.automatic_enabled()
                or not flow.soloist_has_active_playback()
            ):
                raise RuntimeError(
                    "player fault injection requires active managed Spotify playback"
                )
            queue = bridge.request("/queue")
            if not isinstance(queue, dict) or not all(
                item.get("path") == "/srv/media/spotify.pcm"
                for item in queue.get("items", [])
            ):
                raise RuntimeError("OwnTone queue is not exclusively the bridge pipe")
            bridge.request("/player/pause", method="PUT")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fault",
        required=True,
        choices=(
            "capture",
            "owntone",
            "shairport",
            "supervisor",
            "volume",
            "outputs",
            "player",
        ),
    )
    parser.add_argument("--confirm-interruption", action="store_true")
    args = parser.parse_args()
    if not args.confirm_interruption:
        parser.error("live faults require --confirm-interruption")
    if not ready():
        raise RuntimeError("start a live fault test only from a healthy baseline")
    identity = source_identity()
    playing = flow.soloist_has_active_playback()
    start = time.monotonic()
    wall_start = time.time()
    record = {
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "fault": args.fault,
        "active_playback_at_start": playing,
        "success": False,
        "source_process_preserved": False,
    }
    print(
        f"Injecting {args.fault}; Spotify active={playing}. Waiting for automatic recovery.",
        flush=True,
    )
    try:
        inject(args.fault)
        deadline = start + 150
        while time.monotonic() < deadline:
            if source_identity() != identity:
                raise RuntimeError("Spotify source was restarted by a downstream fault")
            try:
                status = json.loads(
                    (PROJECT / "runtime/supervisor-status.json").read_text()
                )
                healthy_state = status["state"] == ("playing" if playing else "idle")
                if ready() and healthy_state and status["updated_at"] > wall_start + 5:
                    if playing and (
                        not flow.soloist_has_active_playback()
                        or flow.check_flow(5, verbose=False)
                    ):
                        time.sleep(2)
                        continue
                    record["success"] = True
                    record["source_process_preserved"] = True
                    record["supervisor_state"] = status["state"]
                    break
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                pass
            time.sleep(2)
        if not record["success"]:
            raise RuntimeError("recovery did not complete within 150 seconds")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        record["error"] = str(exc)
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - start, 1)
        report = PROJECT / "runtime/recovery-verification.jsonl"
        with report.open("a", encoding="utf-8") as handle:
            report.chmod(0o600)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        print(json.dumps(record, indent=2), flush=True)
    return 0 if record["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
