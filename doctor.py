#!/usr/bin/env python3
"""Read-only health audit for the PC + WiiM Spotify bridge."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import ssl
import stat
import subprocess  # nosec B404
import urllib.parse
import urllib.request

from bridge_config import CONFIG, PROJECT

# subprocess is used only for fixed local diagnostic argv; no shell is invoked.

HOME_KEY = CONFIG.soloist_key_file
PROJECT_KEY = PROJECT / ".secrets/soloist_api_key"
LOCAL_ENV = PROJECT / ".env"
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
    # All call sites use controlled argv values; shell execution is disabled.
    completed = subprocess.run(  # nosec B603
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
    # WiiM devices use self-signed LAN certificates. Config validation restricts
    # this destination to an IPv4 address inside TRUSTED_NETWORK.
    context = ssl._create_unverified_context()  # nosec B323
    with urllib.request.urlopen(  # nosec B310
        request, timeout=5, context=context
    ) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("unexpected WiiM response")
    return payload


def owntone_outputs() -> list[dict[str, object]]:
    # This is a fixed loopback HTTP origin.
    with urllib.request.urlopen(  # nosec B310
        "http://127.0.0.1:3689/api/outputs", timeout=5
    ) as response:
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
        if not LOCAL_ENV.is_file():
            report("FAIL", f"missing local configuration: {LOCAL_ENV}")
            return
        env_mode = stat.S_IMODE(LOCAL_ENV.stat().st_mode)
        if env_mode != 0o600:
            report("FAIL", f"local configuration mode is {env_mode:o}, expected 600")
            return
        if (PROJECT / ".git").exists():
            run("git", "check-ignore", "-q", str(PROJECT_KEY))
            run("git", "check-ignore", "-q", str(LOCAL_ENV))
        dockerignore = (
            (PROJECT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        )
        missing_ignores = {".secrets", ".env"}.difference(dockerignore)
        if missing_ignores:
            report(
                "FAIL",
                "sensitive paths missing from .dockerignore: "
                + ", ".join(sorted(missing_ignores)),
            )
            return
        report(
            "PASS",
            "API key and local config use mode 600 and are ignored by Git and Docker",
        )
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
        main_pid = run(
            "systemctl",
            "--user",
            "show",
            "wiim-pc-bridge.service",
            "--property=MainPID",
            "--value",
        )
        monitor_running = main_pid.isdigit() and int(main_pid) > 0
        if (
            linger == "yes"
            and enabled == "enabled"
            and active == "active"
            and monitor_running
        ):
            report(
                "PASS",
                "boot service and volume handoff monitor are active with user "
                "lingering enabled",
            )
        else:
            report(
                "FAIL",
                "boot service state is "
                f"linger={linger}, enabled={enabled}, active={active}, "
                f"monitor_pid={main_pid or 'none'}",
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
        sink_rows = [
            fields
            for line in short_sinks.splitlines()
            if len(fields := line.split()) >= 2
        ]
        default_line = next(
            (fields for fields in sink_rows if fields[1] == EXPECTED_DEFAULT_SINK),
            None,
        )
        bridge_line = next(
            (fields for fields in sink_rows if fields[1] == EXPECTED_SINK),
            None,
        )
        if default_line is None:
            report("FAIL", "RTX DisplayPort PipeWire sink is missing")
            return
        if bridge_line is None:
            report("FAIL", "private wiim_bridge sink is missing")
            return
        default_id = default_line[0]
        bridge_id = bridge_line[0]
        source_outputs = run("pactl", "list", "source-outputs")
        if 'application.name = "parec"' not in source_outputs:
            report("FAIL", "PCM capture stream is missing")
        elif (
            'node.latency = "4410/44100"' not in source_outputs
            or 'pulse.attr.fragsize = "17640"' not in source_outputs
        ):
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
        kitchen_matches = [
            item
            for item in outputs
            if item.get("name") == CONFIG.wiim_output_name
            and item.get("type") == "AirPlay 2"
        ]
        local_matches = [
            item
            for item in outputs
            if item.get("name") == CONFIG.local_output_name
            and item.get("type") == "AirPlay 1"
        ]
        if len(kitchen_matches) != 1:
            report(
                "FAIL",
                f"expected one AirPlay 2 output named {CONFIG.wiim_output_name!r}; "
                f"found {len(kitchen_matches)}",
            )
        if len(local_matches) != 1:
            report(
                "FAIL",
                f"expected one AirPlay 1 output named {CONFIG.local_output_name!r}; "
                f"found {len(local_matches)}",
            )
        if len(kitchen_matches) != 1 or len(local_matches) != 1:
            return

        kitchen = kitchen_matches[0]
        local = local_matches[0]
        report("PASS", "OwnTone output identities and types match the tested topology")

        independently_selected_followers = [
            item
            for item in outputs
            if item.get("name") == CONFIG.living_room_device_name
            and item.get("selected")
        ]
        if independently_selected_followers:
            report(
                "FAIL",
                f"{CONFIG.living_room_device_name} is selected independently",
            )

        expected_ids = {str(kitchen["id"]), str(local["id"])}
        selected_ids = {
            str(item["id"]) for item in outputs if bool(item.get("selected"))
        }
        if selected_ids == expected_ids:
            report("PASS", "exactly the PC and WiiM leader outputs are selected")
        else:
            report("WARN", "selected OwnTone outputs differ from the desired pair")

        if int(local.get("volume", -1)) == CONFIG.local_volume:
            report("PASS", f"PC output volume is {CONFIG.local_volume}%")
        else:
            report("FAIL", f"PC output volume is not {CONFIG.local_volume}%")
        if int(kitchen.get("volume", -1)) == CONFIG.wiim_volume:
            report("PASS", f"WiiM output volume is {CONFIG.wiim_volume}%")
        else:
            report("FAIL", f"WiiM output volume is not {CONFIG.wiim_volume}%")
        expected_offsets = (
            (local, CONFIG.local_output_name, CONFIG.local_offset_ms),
            (kitchen, CONFIG.wiim_output_name, CONFIG.wiim_offset_ms),
        )
        for output, name, desired in expected_offsets:
            if int(output.get("offset_ms", -9999)) == desired:
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
            report(
                "PASS",
                f"{CONFIG.kitchen_device_name} leads one native follower: "
                f"{CONFIG.living_room_device_name}",
            )
        else:
            report(
                "FAIL",
                f"WiiM native group topology is not {CONFIG.kitchen_device_name} -> "
                f"{CONFIG.living_room_device_name}",
            )
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
        version = run(
            "docker", "exec", "wiim-pc-bridge-soloist", "soloist", "--version"
        )
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
    failures.clear()
    warnings.clear()
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
