#!/usr/bin/env python3
"""Read-only health audit for the PC + WiiM Spotify bridge."""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import stat
import subprocess  # nosec B404

import bridge
from bridge_config import PROJECT, ConfigError, get_config

# subprocess is used only for fixed local diagnostic argv; no shell is invoked.

CONTAINERS = (
    "wiim-pc-bridge-owntone",
    "wiim-pc-bridge-shairport",
    "wiim-pc-bridge-soloist",
)
SINK_FIELD = re.compile(r"^\s*Sink:\s*(\d+)\s*$", re.MULTILINE)
BUILD_DATE = re.compile(r"\((\d{8})\)")
EXPIRY_DAYS = 90

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
    """True when a clone of this repository would carry the file."""
    return is_tracked(path) or not is_ignored(path)


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
    key_file = config.soloist_key_file
    local_env = PROJECT / ".env"
    try:
        try:
            info = key_file.stat()
        except OSError:
            report("FAIL", f"missing API key: {key_file}")
            return
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            report("FAIL", f"API key is empty or not a regular file: {key_file}")
            return
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o600:
            report("FAIL", f"API key mode is {mode:o}, expected 600: {key_file}")
            return
        if (PROJECT / ".git").exists() and is_published(key_file):
            report(
                "FAIL",
                f"API key {key_file} would be published by a clone of this repository",
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
            "wiim-pc-bridge.service",
            "--property=MainPID",
            "--value",
        )
        monitor_running = main_pid.isdigit() and int(main_pid) > 0
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


def block_sink_id(block: str) -> str | None:
    match = SINK_FIELD.search(block)
    return match.group(1) if match else None


def check_audio_routes() -> None:
    config = get_config()
    expected_default = config.displayport_sink
    expected_bridge = config.bridge_sink
    try:
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

        source_blocks = run("pactl", "list", "source-outputs").split("\n\n")
        capture = [
            block for block in source_blocks if 'application.name = "parec"' in block
        ]
        if not capture:
            report("FAIL", "PCM capture stream is missing")
        elif not all(
            'node.latency = "4410/44100"' in block
            and 'pulse.attr.fragsize = "17640"' in block
            for block in capture
        ):
            # Check within the parec record; an unrelated stream elsewhere in
            # the dump must not satisfy this.
            report("FAIL", "PCM capture is not using the tested 100 ms buffer")
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
            elif all(block_sink_id(block) == expected_id for block in blocks):
                report("PASS", f"{binary} is routed only to {label}")
            else:
                report("FAIL", f"{binary} is not routed to {label}")
    except Exception as exc:
        report("FAIL", f"audio route checks failed: {exc}")


def check_outputs() -> None:
    config = get_config()
    try:
        outputs = owntone_outputs()
        try:
            local = bridge.resolve_target(outputs, "local")
            wiim = bridge.resolve_target(outputs, "wiim")
        except bridge.BridgeError as exc:
            report("FAIL", str(exc))
            return
        report("PASS", "OwnTone output identities and types match the tested topology")

        expected_ids = {bridge.output_id(local), bridge.output_id(wiim)}
        selected = [item for item in outputs if bool(item.get("selected"))]
        selected_ids = {bridge.output_id(item) for item in selected}
        if selected_ids == expected_ids:
            report("PASS", "exactly the PC and WiiM leader outputs are selected")
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
    check_audio_routes()
    check_outputs()
    check_topology()
    check_soloist_expiry()
    print(f"\nSummary: {len(failures)} failure(s), {len(warnings)} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
