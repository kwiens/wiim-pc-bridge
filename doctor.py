#!/usr/bin/env python3
"""Read-only health audit for the PC + WiiM Spotify bridge."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import time
from pathlib import Path

import audio_flow_check
import bridge
import bridge_supervisor
import output_control
from bridge_config import PROJECT, ConfigError, get_config

# subprocess is used only for fixed local diagnostic argv; no shell is invoked.

CONTAINERS = (
    "wiim-pc-bridge-owntone",
    "wiim-pc-bridge-shairport",
    "wiim-pc-bridge-soloist",
    "wiim-pc-bridge-capture",
)
SINK_FIELD = re.compile(r"^\s*Sink:\s*(\d+)\s*$", re.MULTILINE)
SOURCE_FIELD = re.compile(r"^\s*Source:\s*(\d+)\s*$", re.MULTILINE)
BUILD_DATE = re.compile(r"\((\d{8})\)")
EXPIRY_DAYS = 90
# Fixed supervisor state path inside this project's Soloist container.

failures: list[str] = []
warnings: list[str] = []


def report(level: str, message: str) -> None:
    print(f"{level:4} {message}")
    if level == "FAIL":
        failures.append(message)
    elif level == "WARN":
        warnings.append(message)


def run(*args: str, timeout: int = 10) -> str:
    """Run a diagnostic command whose non-zero exit is a genuine error."""
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


def probe(*args: str, timeout: int = 10) -> str:
    """Run a state query whose non-zero exit IS the answer, not a failure.

    `systemctl is-active` exits 3 for "inactive" and `loginctl show-user` exits
    non-zero for a user with no lingering session. Treating those as errors
    would turn every unhealthy state into an exception.
    """
    try:
        completed = subprocess.run(  # nosec B603
            args,
            cwd=PROJECT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable ({exc})"
    return completed.stdout.strip() or "unknown"


def git_command(*args: str) -> list[str]:
    """Resolve Git to an absolute path so no PATH entry can shadow it."""
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required for the repository checks")
    return [executable, *args]


def is_ignored(path: object) -> bool:
    completed = subprocess.run(  # nosec B603
        git_command("check-ignore", "-q", str(path)),
        cwd=PROJECT,
        check=False,
        capture_output=True,
        timeout=10,
    )
    return completed.returncode == 0


def is_tracked(path: object) -> bool:
    completed = subprocess.run(  # nosec B603
        git_command("ls-files", "--cached", "--error-unmatch", "--", str(path)),
        cwd=PROJECT,
        check=False,
        capture_output=True,
        timeout=10,
    )
    return completed.returncode == 0


def is_published(path: object) -> bool:
    """True when a clone of this repository would carry the file.

    A path outside the working tree cannot be published by this repository at
    all, and `git check-ignore` reports it as "not ignored", so it has to be
    excluded before that answer is consulted.
    """
    resolved = Path(path).resolve()
    if resolved != PROJECT and PROJECT not in resolved.parents:
        return False
    return is_tracked(resolved) or not is_ignored(resolved)


def owntone_outputs() -> list[dict[str, object]]:
    return bridge.outputs()


def ignore_entries(text: str) -> set[str]:
    """Normalise ignore-file lines so `.secrets` and `.secrets/` compare equal."""
    entries = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        entries.add(line.strip("/"))
    return entries


def check_secrets() -> None:
    config = get_config()
    local_env = PROJECT / ".env"
    try:
        for label, credential in (
            ("API key", config.soloist_key_file),
            ("Pulse cookie", config.pulse_cookie_path),
        ):
            try:
                info = credential.stat()
            except OSError:
                report("FAIL", f"missing {label}: {credential}")
                return
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                report("FAIL", f"{label} is empty or not a regular file: {credential}")
                return
            mode = stat.S_IMODE(info.st_mode)
            if mode != 0o600:
                report("FAIL", f"{label} mode is {mode:o}, expected 600: {credential}")
                return
            if (PROJECT / ".git").exists() and is_published(credential):
                report(
                    "FAIL",
                    f"{label} {credential} would be published by a clone of this "
                    "repository",
                )
                return

        try:
            env_info = local_env.stat()
        except OSError:
            report("FAIL", f"missing local configuration: {local_env}")
            return
        env_mode = stat.S_IMODE(env_info.st_mode)
        if env_mode != 0o600:
            report("FAIL", f"local configuration mode is {env_mode:o}, expected 600")
            return
        if (PROJECT / ".git").exists() and is_published(local_env):
            report("FAIL", f"{local_env} would be published by a clone")
            return

        # Compare against the committed ignore files, not just `git check-ignore`,
        # which also honours machine-local ignore configuration a fresh clone
        # will not have.
        for name, required in (
            (".gitignore", {".env", "cache", "runtime"}),
            (".dockerignore", {".env", "cache", "runtime", ".git"}),
        ):
            entries = ignore_entries((PROJECT / name).read_text(encoding="utf-8"))
            missing = required.difference(entries)
            if missing:
                report(
                    "FAIL",
                    f"sensitive paths missing from {name}: "
                    + ", ".join(sorted(missing)),
                )
                return
        report(
            "PASS",
            "credentials and local config use mode 600 and are not publishable",
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
                "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}"
                "{{else}}none{{end}}",
                container,
            ).split()
            if not state or state[0] != "running":
                report("FAIL", f"{container} is not running")
            elif len(state) < 2 or state[1] == "none":
                # A container with no health config must not be reported as
                # healthy: nothing has actually been checked.
                report("WARN", f"{container} is running but declares no health check")
            elif state[1] != "healthy":
                report("FAIL", f"{container} health is {state[1]}")
            else:
                report("PASS", f"{container} is healthy")
        except Exception as exc:
            report("FAIL", f"could not inspect {container}: {exc}")


def check_startup() -> None:
    try:
        linger = probe(
            "loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"
        )
        enabled = probe("systemctl", "--user", "is-enabled", "wiim-pc-bridge.service")
        active = probe("systemctl", "--user", "is-active", "wiim-pc-bridge.service")
        main_pid = probe(
            "systemctl",
            "--user",
            "show",
            "wiim-pc-bridge-supervisor.service",
            "--property=MainPID",
            "--value",
        )
        monitor_running = main_pid.isdigit() and int(main_pid) > 0
        volume_active = probe(
            "systemctl", "--user", "is-active", "wiim-pc-bridge-volume.service"
        )
        if enabled == "not-found":
            # The boot service is an optional install step, not a fault.
            report(
                "WARN",
                "boot service is not installed; run ./install-service.sh to start "
                "the bridge automatically",
            )
        elif (
            linger == "yes"
            and enabled == "enabled"
            and active == "active"
            and monitor_running
            and volume_active == "active"
        ):
            report(
                "PASS",
                "stack, independent supervisor, and volume helper are active with user "
                "lingering enabled",
            )
        else:
            report(
                "FAIL",
                "boot service state is "
                f"linger={linger}, enabled={enabled}, active={active}, "
                f"monitor_pid={main_pid or 'none'}, volume={volume_active}",
            )

        wrong_policy = []
        for container in CONTAINERS:
            try:
                policy = run(
                    "docker",
                    "inspect",
                    "--format",
                    "{{.HostConfig.RestartPolicy.Name}}",
                    container,
                )
            except Exception as exc:
                report("FAIL", f"could not read {container} restart policy: {exc}")
                continue
            if policy != "unless-stopped":
                wrong_policy.append(f"{container}={policy or 'unset'}")
        if wrong_policy:
            report(
                "FAIL",
                "restart policy is not unless-stopped: " + ", ".join(wrong_policy),
            )
        else:
            report("PASS", "all bridge containers use unless-stopped restart policy")
    except Exception as exc:
        report("FAIL", f"startup checks failed: {exc}")


def check_supervisor() -> None:
    path = PROJECT / "runtime/supervisor-status.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(state["updated_at"])
        if age < -5 or age > 120:
            report(
                "FAIL",
                "supervisor heartbeat is stale; process health alone is insufficient",
            )
        elif state["state"] in ("degraded", "stopped"):
            report("FAIL", f"supervisor {state['state']}: {state['detail']}")
        elif state["state"] in ("starting", "recovering", "connecting", "unverified"):
            report("WARN", f"supervisor {state['state']}: {state['detail']}")
        else:
            report("PASS", f"supervisor {state['state']}: {state['detail']}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report("FAIL", f"supervisor state unavailable: {exc}")


def block_sink_id(block: str) -> str | None:
    match = SINK_FIELD.search(block)
    return match.group(1) if match else None


def block_source_id(block: str) -> str | None:
    match = SOURCE_FIELD.search(block)
    return match.group(1) if match else None


def block_property(block: str, name: str) -> str | None:
    match = re.search(
        rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"\s*$', block, re.MULTILINE
    )
    return match.group(1) if match else None


def block_muted(block: str) -> bool:
    return re.search(r"^\s*Mute:\s*yes\s*$", block, re.MULTILINE) is not None


def block_corked(block: str) -> bool:
    return re.search(r"^\s*Corked:\s*yes\s*$", block, re.MULTILINE) is not None


def check_audio_routes() -> None:
    config = get_config()
    expected_default = config.displayport_sink
    expected_bridge = config.bridge_sink
    try:
        if audio_flow_check.runtime_socket_mounts_current():
            report("PASS", "container audio socket mounts match the live host sockets")
        else:
            report(
                "FAIL",
                "a container has a stale or unavailable PipeWire/Pulse socket mount",
            )

        default_sink = run("pactl", "get-default-sink")
        if default_sink != expected_default:
            report(
                "WARN",
                f"host default sink is {default_sink}, expected {expected_default}",
            )
        else:
            report("PASS", f"host default sink is {expected_default}")

        short_sinks = run("pactl", "list", "short", "sinks")
        sink_rows = [
            fields
            for line in short_sinks.splitlines()
            if len(fields := line.split()) >= 2
        ]
        default_line = next(
            (fields for fields in sink_rows if fields[1] == expected_default),
            None,
        )
        bridge_line = next(
            (fields for fields in sink_rows if fields[1] == expected_bridge),
            None,
        )
        if default_line is None:
            report("FAIL", f"PipeWire sink {expected_default} is missing")
            return
        if bridge_line is None:
            report("FAIL", f"private bridge sink {expected_bridge} is missing")
            return
        default_id = default_line[0]
        bridge_id = bridge_line[0]
        source_rows = [
            fields
            for line in run("pactl", "list", "short", "sources").splitlines()
            if len(fields := line.split()) >= 2
        ]
        bridge_monitor = next(
            (
                fields
                for fields in source_rows
                if fields[1] == expected_bridge + ".monitor"
            ),
            None,
        )
        if bridge_monitor is None:
            report(
                "FAIL", f"private bridge monitor {expected_bridge}.monitor is missing"
            )
            return

        capture_process = run(
            "docker",
            "exec",
            "wiim-pc-bridge-capture",
            "cat",
            "/proc/1/comm",
        )
        if capture_process != "python3":
            report(
                "FAIL", "capture relay is not running as the container's main process"
            )
            return
        source_blocks = run("pactl", "list", "source-outputs").split("\n\n")
        capture = [
            block
            for block in source_blocks
            if block_property(block, "application.name") == "parec"
            and block_property(block, "application.id") == "wiim-pc-bridge.capture"
        ]
        if len(capture) != 1:
            report("FAIL", "PCM capture stream is missing")
        elif not (
            block_property(capture[0], "target.object") == expected_bridge
            and block_source_id(capture[0]) == bridge_monitor[0]
            and "Sample Specification: s16le 2ch 44100Hz" in capture[0]
            and 'node.latency = "4410/44100"' in capture[0]
            and 'pulse.attr.fragsize = "17640"' in capture[0]
        ):
            report(
                "FAIL",
                f"PCM capture is not routed from {expected_bridge} at the tested "
                "44.1 kHz/100 ms format",
            )
        else:
            report("PASS", "PCM capture is 44.1 kHz stereo with a 100 ms buffer")

        sink_blocks = run("pactl", "list", "sink-inputs").split("\n\n")
        for binary, expected_id, label in (
            ("soloist", bridge_id, f"the private bridge sink {expected_bridge}"),
            ("shairport-sync", default_id, f"the PC sink {expected_default}"),
        ):
            blocks = [
                block
                for block in sink_blocks
                if f'application.process.binary = "{binary}"' in block
            ]
            if not blocks:
                report("WARN", f"{binary} is idle; no live route to verify")
            elif len(blocks) > 1:
                report(
                    "FAIL",
                    f"{binary} has {len(blocks)} simultaneous sink inputs; "
                    "expected exactly one",
                )
            elif block_sink_id(blocks[0]) == expected_id:
                report("PASS", f"{binary} is routed only to {label}")
                if binary == "shairport-sync" and block_muted(blocks[0]):
                    report(
                        "FAIL"
                        if audio_flow_check.soloist_has_active_playback()
                        else "WARN",
                        "local Shairport receiver is muted; PC bridge audio is blocked",
                    )
                elif (
                    binary == "shairport-sync"
                    and block_corked(blocks[0])
                    and audio_flow_check.soloist_has_active_playback()
                ):
                    report(
                        "FAIL",
                        "local Shairport receiver is still corked during active playback",
                    )
            else:
                report("FAIL", f"{binary} is not routed to {label}")
    except Exception as exc:
        report("FAIL", f"audio route checks failed: {exc}")


def check_desktop_spotify_audio() -> None:
    """Flag a silent desktop player that can hide behind a healthy bridge."""
    try:
        if audio_flow_check.soloist_has_active_playback():
            return  # The bridge intentionally mutes a conflicting desktop player.
        streams = bridge_supervisor.Runtime().desktop_spotify_streams()
        if any(stream["mute"] for stream in streams):
            report(
                "WARN",
                "desktop Spotify is muted while the bridge is idle; "
                "direct PC playback may be silent",
            )
    except Exception as exc:
        report("WARN", f"desktop Spotify mute check unavailable: {exc}")


def check_outputs() -> None:
    config = get_config()
    try:
        outputs = owntone_outputs()
        try:
            local, wiim = bridge.resolve_pair(outputs)
        except bridge.BridgeError as exc:
            report("FAIL", str(exc))
            return
        report("PASS", "OwnTone output identities and types match the tested topology")

        expected_ids = {bridge.output_id(local), bridge.output_id(wiim)}
        selected = [item for item in outputs if item.get("selected") is True]
        selected_ids = {bridge.output_id(item) for item in selected}
        if selected_ids == expected_ids:
            report("PASS", "exactly the PC and WiiM leader outputs are selected")
        elif not output_control.automatic_enabled() and selected_ids.issubset(
            {bridge.output_id(local)}
        ):
            report(
                "PASS",
                "explicit manual output selection is active; automatic reconnection is suspended",
            )
        elif not selected_ids:
            try:
                playing = audio_flow_check.soloist_has_active_playback()
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.SubprocessError,
            ) as exc:
                report("WARN", f"cannot verify idle output selection: {exc}")
            else:
                if playing or config.keepalive_enabled:
                    report(
                        "FAIL",
                        "bridge outputs are disconnected when they should be selected",
                    )
                else:
                    report("PASS", "idle AirPlay outputs are disconnected")
        else:
            # Any unexpected selected output breaks the project's central
            # invariant, including a follower OwnTone discovered on its own.
            unexpected = sorted(
                str(item.get("name"))
                for item in selected
                if bridge.output_id(item) not in expected_ids
            )
            detail = ", ".join(unexpected) if unexpected else "the pair is incomplete"
            report(
                "FAIL",
                f"selected OwnTone outputs differ from the desired pair: {detail}",
            )

        for output, name, desired_volume, desired_offset in (
            (
                local,
                config.local_output_name,
                config.local_volume,
                config.local_offset_ms,
            ),
            (wiim, config.wiim_output_name, config.wiim_volume, config.wiim_offset_ms),
        ):
            try:
                volume = bridge.output_integer(output, "volume")
                offset = bridge.output_integer(output, "offset_ms")
            except bridge.BridgeError as exc:
                report("FAIL", f"{name}: {exc}")
                continue
            if volume == desired_volume:
                report("PASS", f"{name} volume is {desired_volume}%")
            else:
                report(
                    "FAIL", f"{name} volume is {volume}%, expected {desired_volume}%"
                )
            if offset == desired_offset:
                report("PASS", f"{name} offset is {desired_offset} ms")
            else:
                report(
                    "FAIL",
                    f"{name} offset is {offset} ms, expected {desired_offset} ms",
                )
    except Exception as exc:
        report("FAIL", f"OwnTone output checks failed: {exc}")


def check_topology() -> None:
    config = get_config()
    try:
        bridge.validate_wiim_group()
    except bridge.BridgeError as exc:
        report("FAIL", str(exc))
        return
    except Exception as exc:
        report("FAIL", f"WiiM topology checks failed: {exc}")
        return
    report(
        "PASS",
        f"{config.kitchen_device_name} leads the configured group: "
        f"{bridge.describe(set(config.followers))}",
    )


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
    except Exception as exc:
        report("FAIL", f"Soloist is not reachable: {exc}")
        return
    if "logged in: yes" not in status_output:
        report("FAIL", "Soloist is reachable but not logged in")
    else:
        report("PASS", "Soloist is reachable and logged in")
    try:
        version = run(
            "docker", "exec", "wiim-pc-bridge-soloist", "soloist", "--version"
        )
    except Exception as exc:
        report("WARN", f"could not read the Soloist version: {exc}")
        return
    match = BUILD_DATE.search(version)
    if not match:
        report("WARN", f"could not read Soloist build date from: {version}")
        return
    try:
        build_date = dt.datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        report("WARN", f"Soloist reported an invalid build date: {match.group(1)}")
        return
    days = (build_date + dt.timedelta(days=EXPIRY_DAYS) - dt.date.today()).days
    if days <= 0:
        report("FAIL", f"Soloist build has reached its {EXPIRY_DAYS}-day expiry")
    elif days <= 14:
        report("WARN", f"Soloist build expires in {days} days; update it now")
    else:
        report("PASS", f"Soloist build has approximately {days} days before expiry")


def main() -> int:
    failures.clear()
    warnings.clear()
    print("PC + WiiM bridge health audit")
    try:
        get_config()
    except ConfigError as exc:
        print(f"FAIL invalid configuration: {exc}")
        print("\nSummary: 1 failure(s), 0 warning(s)")
        return 1
    check_secrets()
    check_containers()
    check_startup()
    check_supervisor()
    check_audio_routes()
    check_desktop_spotify_audio()
    check_outputs()
    check_topology()
    check_soloist_expiry()
    print(f"\nSummary: {len(failures)} failure(s), {len(warnings)} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
