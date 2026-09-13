#!/usr/bin/env python3
"""Read-only health audit for the PC + WiiM Spotify bridge."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import urllib.parse
import urllib.request

from bridge_config import CONFIG, PROJECT

HOME_KEY = CONFIG.soloist_key_file
PROJECT_KEY = PROJECT / ".secrets/soloist_api_key"
KITCHEN_IP = CONFIG.kitchen_ip
LIVING_ROOM_IP = CONFIG.living_room_ip
EXPECTED_SINK = CONFIG.bridge_sink
EXPECTED_DEFAULT_SINK = CONFIG.displayport_sink
CONTAINERS = (
    "wiim-pc-bridge-owntone",
    "wiim-pc-bridge-shairport",
    "wiim-pc-bridge-soloist",
)

failures: list[str] = []
warnings: list[str] = []


def report(level: str, message: str) -> None:
    print(f"{level:4} {message}")
    if level == "FAIL":
        failures.append(message)
    elif level == "WARN":
        warnings.append(message)


def run(*args: str, timeout: int = 10) -> str:
    completed = subprocess.run(
        args,
        cwd=PROJECT,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip()


def wiim_json(ip: str, command: str) -> dict[str, object]:
    encoded = urllib.parse.quote(command, safe="")
    request = urllib.request.Request(
        f"https://{ip}/httpapi.asp?command={encoded}", method="GET"
    )
    context = ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=5, context=context) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("unexpected WiiM response")
    return payload


def owntone_outputs() -> list[dict[str, object]]:
    with urllib.request.urlopen("http://127.0.0.1:3689/api/outputs", timeout=5) as response:
        payload = json.load(response)
    outputs = payload.get("outputs") if isinstance(payload, dict) else None
    if not isinstance(outputs, list):
        raise ValueError("unexpected OwnTone outputs response")
    return outputs


def check_secrets() -> None:
    try:
        for path in (HOME_KEY, PROJECT_KEY):
            if not path.is_file() or path.stat().st_size == 0:
                report("FAIL", f"missing API key: {path}")
                return
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != 0o600:
                report("FAIL", f"API key mode is {mode:o}, expected 600: {path}")
                return
        if HOME_KEY.read_bytes() != PROJECT_KEY.read_bytes():
            report("FAIL", "home and project API key copies differ")
            return
        run("git", "check-ignore", "-q", str(PROJECT_KEY))
        dockerignore = (PROJECT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if ".secrets" not in dockerignore:
            report("FAIL", ".secrets is not excluded from Docker build context")
            return
        report("PASS", "API key copies match, mode 600, ignored by Git and Docker")
    except Exception as exc:
        report("FAIL", f"secret checks failed: {exc}")


def check_containers() -> None:
    for container in CONTAINERS:
        try:
            state = run(
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                container,
            ).split()
            if not state or state[0] != "running":
                report("FAIL", f"{container} is not running")
            elif len(state) > 1 and state[1] != "healthy":
                report("FAIL", f"{container} health is {state[1]}")
            else:
                report("PASS", f"{container} is healthy")
        except Exception as exc:
            report("FAIL", f"could not inspect {container}: {exc}")


def check_startup() -> None:
    try:
        linger = run(
            "loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"
        )
        enabled = run("systemctl", "--user", "is-enabled", "wiim-pc-bridge.service")
        active = run("systemctl", "--user", "is-active", "wiim-pc-bridge.service")
        if linger == "yes" and enabled == "enabled" and active == "active":
            report("PASS", "boot service is active with user lingering enabled")
        else:
            report(
                "FAIL",
                f"boot service state is linger={linger}, enabled={enabled}, active={active}",
            )
        for container in CONTAINERS:
            policy = run(
                "docker",
                "inspect",
                "--format",
                "{{.HostConfig.RestartPolicy.Name}}",
                container,
            )
            if policy != "unless-stopped":
                report("FAIL", f"{container} restart policy is {policy or 'unset'}")
                return
        report("PASS", "all bridge containers use unless-stopped restart policy")
    except Exception as exc:
        report("FAIL", f"startup checks failed: {exc}")


def check_audio_routes() -> None:
    try:
        default_sink = run("pactl", "get-default-sink")
        if default_sink != EXPECTED_DEFAULT_SINK:
            report("WARN", f"host default sink is currently {default_sink}")
        else:
            report("PASS", "host default sink remains RTX DisplayPort")

        short_sinks = run("pactl", "list", "short", "sinks")
        default_line = next(
            (
                line
                for line in short_sinks.splitlines()
                if line.split()[1] == EXPECTED_DEFAULT_SINK
            ),
            None,
        )
        bridge_line = next(
            (line for line in short_sinks.splitlines() if line.split()[1] == EXPECTED_SINK),
            None,
        )
        if default_line is None:
            report("FAIL", "RTX DisplayPort PipeWire sink is missing")
            return
        if bridge_line is None:
            report("FAIL", "private wiim_bridge sink is missing")
            return
        default_id = default_line.split()[0]
        bridge_id = bridge_line.split()[0]
        source_outputs = run("pactl", "list", "source-outputs")
        if 'application.name = "parec"' not in source_outputs:
            report("FAIL", "PCM capture stream is missing")
        elif 'node.latency = "4410/44100"' not in source_outputs or \
                'pulse.attr.fragsize = "17640"' not in source_outputs:
            report("FAIL", "PCM capture is not using the tested 100 ms buffer")
        else:
            report("PASS", "PCM capture is 44.1 kHz stereo with a 100 ms buffer")

        sink_inputs = run("pactl", "list", "sink-inputs")
        soloist_blocks = [
            block
            for block in sink_inputs.split("\n\n")
            if 'application.process.binary = "soloist"' in block
        ]
        if not soloist_blocks:
            report("WARN", "Soloist is idle; no live sink route to verify")
        elif all(f"Sink: {bridge_id}" in block for block in soloist_blocks):
            report("PASS", "Soloist is routed only to the private bridge sink")
        else:
            report("FAIL", "Soloist is bypassing the private bridge sink")

        shairport_blocks = [
            block
            for block in sink_inputs.split("\n\n")
            if 'application.process.binary = "shairport-sync"' in block
        ]
        if not shairport_blocks:
            report("WARN", "Shairport is idle; no live DisplayPort route to verify")
        elif all(f"Sink: {default_id}" in block for block in shairport_blocks):
            report("PASS", "Shairport shares the RTX DisplayPort PipeWire sink")
        else:
            report("FAIL", "Shairport is not routed to RTX DisplayPort")
    except Exception as exc:
        report("FAIL", f"audio route checks failed: {exc}")


def check_outputs() -> None:
    try:
        outputs = owntone_outputs()
        by_name = {str(item.get("name")): item for item in outputs}
        kitchen = by_name.get(CONFIG.wiim_output_name)
        local = by_name.get(CONFIG.local_output_name)
        if not kitchen or kitchen.get("type") != "AirPlay 2":
            report("FAIL", "Kitchen is not configured as the single AirPlay 2 leader output")
        elif not local or local.get("type") != "AirPlay 1":
            report("FAIL", "local DisplayPort receiver is not classic AirPlay")
        else:
            report("PASS", "OwnTone output types match the tested topology")
        living = by_name.get("Living Room")
        if living and living.get("selected"):
            report("FAIL", "Living Room is selected independently")
        elif kitchen and local and kitchen.get("selected") and local.get("selected"):
            report("PASS", "only the PC and Kitchen leader outputs are selected")
        else:
            report("WARN", "bridge outputs are not both selected")
        if local and int(local.get("volume", -1)) == CONFIG.local_volume:
            report("PASS", f"PC output volume is {CONFIG.local_volume}%")
        else:
            report("FAIL", f"PC output volume is not {CONFIG.local_volume}%")
        if kitchen and int(kitchen.get("volume", -1)) == CONFIG.wiim_volume:
            report("PASS", f"WiiM output volume is {CONFIG.wiim_volume}%")
        else:
            report("FAIL", f"WiiM output volume is not {CONFIG.wiim_volume}%")
        expected_offsets = (
            (local, CONFIG.local_output_name, CONFIG.local_offset_ms),
            (kitchen, CONFIG.wiim_output_name, CONFIG.wiim_offset_ms),
        )
        for output, name, desired in expected_offsets:
            if output and int(output.get("offset_ms", -9999)) == desired:
                report("PASS", f"{name} offset is {desired} ms")
            else:
                report("FAIL", f"{name} offset is not {desired} ms")
    except Exception as exc:
        report("FAIL", f"OwnTone output checks failed: {exc}")


def check_topology() -> None:
    try:
        kitchen = wiim_json(KITCHEN_IP, "getStatusEx")
        living = wiim_json(LIVING_ROOM_IP, "getStatusEx")
        followers = wiim_json(KITCHEN_IP, "multiroom:getSlaveList")
        follower_list = followers.get("slave_list")
        expected_follower = isinstance(follower_list, list) and any(
            isinstance(item, dict)
            and item.get("ip") == LIVING_ROOM_IP
            and item.get("name") == CONFIG.living_room_device_name
            for item in follower_list
        )
        healthy = (
            str(kitchen.get("group")) == "0"
            and str(living.get("group")) == "1"
            and living.get("master_ip") == KITCHEN_IP
            and followers.get("slaves") == 1
            and expected_follower
        )
        if healthy:
            report("PASS", "Kitchen leads one native follower: Living Room")
        else:
            report("FAIL", "WiiM native group topology is not Kitchen -> Living Room")
    except Exception as exc:
        report("FAIL", f"WiiM topology checks failed: {exc}")


def check_soloist_expiry() -> None:
    try:
        status_output = run(
            "docker",
            "exec",
            "wiim-pc-bridge-soloist",
            "soloist",
            "ctl",
            "status",
            "--ws",
            "127.0.0.1:9090",
        )
        if "logged in: yes" not in status_output:
            report("FAIL", "Soloist is reachable but not logged in")
        else:
            report("PASS", "Soloist is reachable and logged in")
        version = run("docker", "exec", "wiim-pc-bridge-soloist", "soloist", "--version")
        match = re.search(r"\((\d{8})\)", version)
        if not match:
            report("WARN", f"could not read Soloist build date from: {version}")
            return
        build_date = dt.datetime.strptime(match.group(1), "%Y%m%d").date()
        days = (build_date + dt.timedelta(days=90) - dt.date.today()).days
        if days <= 0:
            report("FAIL", "Soloist build has reached its 90-day expiry")
        elif days <= 14:
            report("WARN", f"Soloist build expires in {days} days; update it now")
        else:
            report("PASS", f"Soloist build has approximately {days} days before expiry")
    except Exception as exc:
        report("FAIL", f"Soloist expiry check failed: {exc}")


def main() -> int:
    print("PC + WiiM bridge health audit")
    check_secrets()
    check_containers()
    check_startup()
    check_audio_routes()
    check_outputs()
    check_topology()
    check_soloist_expiry()
    print(f"\nSummary: {len(failures)} failure(s), {len(warnings)} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
