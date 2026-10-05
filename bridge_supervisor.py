#!/usr/bin/env python3
"""Independent, bounded playback supervision; never cold-restart the stack."""

from __future__ import annotations

import json
import logging
import logging.handlers
import math
import os
import signal
import socket
import subprocess  # nosec B404
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import audio_flow_check as flow
import bridge
import output_control
from bridge_config import PROJECT, get_config
from volume_handoff import IdleOutputController

SERVICES = ("soloist", "owntone", "shairport", "capture")
PERIOD = 5.0
PROBE_PERIOD = 15.0
SETTLE_SECONDS = 20.0
ROUTE_REPAIR_LIMIT = 3
ERRORS = (OSError, RuntimeError, ValueError, subprocess.SubprocessError)
stopping = False


@dataclass(frozen=True)
class Finding:
    kind: str
    detail: str
    services: tuple[str, ...] = ()


def command(argv: list[str], timeout: int = 30) -> str:
    return subprocess.run(  # nosec B603
        argv, cwd=PROJECT, capture_output=True, text=True, check=True, timeout=timeout
    ).stdout


def status_path() -> Path:
    return PROJECT / "runtime/supervisor-status.json"


def pulse_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def write_status(data: dict[str, object]) -> None:
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".supervisor-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.sendto(message.encode(), address)


class Runtime:
    """All production I/O; tests replace boundaries, not decision logic."""

    def __init__(self) -> None:
        self.outputs = IdleOutputController(enabled=not get_config().keepalive_enabled)
        self.outputs.idle_since = time.monotonic() - 60
        self.source_identity: str | None = None

    def infrastructure(self) -> Finding | None:
        config = get_config()
        pulse = str(config.runtime_dir / "pulse/native")
        pipewire = str(config.runtime_dir / "pipewire-0")
        try:
            pulse_id = flow.socket_identity(pulse)
            pipewire_id = flow.socket_identity(pipewire)
            flow.pulse_objects("sinks")
        except ERRORS:
            return Finding(
                "host_unavailable",
                "Waiting for the host audio server; no containers restarted",
            )
        names = set(
            command(
                [
                    "/usr/bin/docker",
                    "ps",
                    "--all",
                    "--filter",
                    "name=^/wiim-pc-bridge-",
                    "--format",
                    "{{.Names}}",
                ],
                timeout=10,
            ).splitlines()
        )
        for service in SERVICES:
            if "wiim-pc-bridge-" + service not in names:
                return Finding(
                    "container_missing", f"{service} container is missing", (service,)
                )
        raw = command(
            [
                "/usr/bin/docker",
                "inspect",
                "--format",
                "{{.Name}} {{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.State.StartedAt}}",
                *["wiim-pc-bridge-" + service for service in SERVICES],
            ],
            timeout=10,
        )
        states = {
            line.split()[0].removeprefix("/wiim-pc-bridge-"): line.split()[1:]
            for line in raw.splitlines()
        }
        source = states.get("soloist", [])
        if len(source) == 3:
            self.source_identity = source[2]
        for service in SERVICES:
            state = states.get(service, [])
            if len(state) != 3 or state[0] != "true":
                return Finding(
                    "container_down", f"{service} is not running", (service,)
                )
        stale = []
        mounts = {
            "soloist": ((pulse, pulse_id), (pipewire, pipewire_id)),
            "capture": ((pulse, pulse_id),),
            "shairport": ((flow.SHAIRPORT_PULSE_SOCKET, pulse_id),),
        }
        for service, endpoints in mounts.items():
            if any(
                flow.container_socket_identity("wiim-pc-bridge-" + service, path)
                != expected
                for path, expected in endpoints
            ):
                stale.append(service)
        if stale:
            return Finding(
                "stale_mount",
                "Audio socket mounts no longer match the host",
                tuple(stale),
            )
        for service, state in states.items():
            if state[1] == "unhealthy":
                return Finding(
                    "container_unhealthy", f"{service} health check failed", (service,)
                )
        return None

    def playing(self) -> bool:
        return flow.soloist_has_active_playback()

    @staticmethod
    def pc_sink_available() -> bool:
        config = get_config()
        return (
            sum(
                sink.get("name") == config.displayport_sink
                for sink in flow.pulse_objects("sinks")
            )
            == 1
        )

    def desktop_spotify_streams(self) -> list[dict[str, object]]:
        """Resolve PipeWire client identity, since Flatpak omits it on streams."""
        snapshot = json.loads(command(["/usr/bin/pw-dump"], timeout=5))
        if not isinstance(snapshot, list):
            raise RuntimeError("PipeWire returned an invalid object list")
        client_ids = {
            str(item.get("id"))
            for item in snapshot
            if isinstance(item, dict)
            and item.get("type") == "PipeWire:Interface:Client"
            and isinstance(item.get("info"), dict)
            and isinstance(item["info"].get("props"), dict)
            and item["info"]["props"].get("pipewire.access.portal.app_id")
            == "com.spotify.Client"
        }
        return [
            item
            for item in flow.pulse_objects("sink-inputs")
            if isinstance(item.get("properties"), dict)
            and str(item["properties"].get("client.id")) in client_ids
            and pulse_index(item.get("index"))
            and isinstance(item.get("mute"), bool)
        ]

    def set_desktop_spotify_mute(self, index: int, mute: bool) -> bool:
        """Change only the exact still-present Flatpak Spotify stream."""
        matches = [
            stream
            for stream in self.desktop_spotify_streams()
            if stream["index"] == index
        ]
        if len(matches) != 1:
            return False
        if matches[0]["mute"] != mute:
            command(
                [
                    "/usr/bin/pactl",
                    "set-sink-input-mute",
                    str(index),
                    "1" if mute else "0",
                ],
                timeout=5,
            )
        return any(
            stream["index"] == index and stream["mute"] == mute
            for stream in self.desktop_spotify_streams()
        )

    def desktop_spotify_in_bridge(self, index: int) -> bool:
        """Move the desktop app off the capture sink when the PC sink exists."""
        config = get_config()
        streams = [
            stream
            for stream in self.desktop_spotify_streams()
            if stream["index"] == index
        ]
        if len(streams) != 1:
            return False
        sinks = flow.pulse_objects("sinks")
        private = [sink for sink in sinks if sink.get("name") == config.bridge_sink]
        if len(private) != 1 or streams[0].get("sink") != private[0].get("index"):
            return False
        physical = [
            sink for sink in sinks if sink.get("name") == config.displayport_sink
        ]
        if len(physical) != 1 or not pulse_index(physical[0].get("index")):
            return True
        command(
            [
                "/usr/bin/pactl",
                "move-sink-input",
                str(index),
                config.displayport_sink,
            ],
            timeout=5,
        )
        return any(
            stream["index"] == index and stream.get("sink") == private[0]["index"]
            for stream in self.desktop_spotify_streams()
        )

    @staticmethod
    def soloist_streams() -> list[dict[str, object]]:
        """Identify only the bridge's Spotify process, never a desktop player."""
        return [
            item
            for item in flow.pulse_objects("sink-inputs")
            if isinstance(item.get("properties"), dict)
            and item["properties"].get("application.name") == "spotify"
            and item["properties"].get("application.process.binary") == "soloist"
        ]

    @staticmethod
    def capture_streams() -> list[dict[str, object]]:
        """Find only the bridge capture process, not another recorder."""
        return [
            item
            for item in flow.pulse_objects("source-outputs")
            if isinstance(item.get("properties"), dict)
            and item["properties"].get("application.name") == "parec"
            and item["properties"].get("application.id") == "wiim-pc-bridge.capture"
        ]

    def capture_route(self) -> Finding | None:
        """Check the actual recording source, not the stale target.object hint."""
        monitor_name = get_config().bridge_sink + ".monitor"
        try:
            monitors = [
                source
                for source in flow.pulse_objects("sources")
                if source.get("name") == monitor_name
                and pulse_index(source.get("index"))
            ]
            streams = self.capture_streams()
        except ERRORS:
            return Finding("host_unavailable", "Cannot inspect the PCM capture route")
        if len(monitors) != 1:
            return Finding("host_unavailable", "Private bridge monitor is unavailable")
        if len(streams) != 1 or not all(
            pulse_index(item.get("index")) and pulse_index(item.get("source"))
            for item in streams
        ):
            return Finding(
                "capture_unknown",
                "PCM capture stream is missing or ambiguous; not moving any recorder",
            )
        if streams[0]["source"] != monitors[0]["index"]:
            return Finding(
                "capture_misrouted",
                "PCM capture is recording a non-bridge source; "
                "desktop/game audio may feed back into the AirPlay outputs",
            )
        return None

    def receiver_route(self) -> Finding | None:
        """Restore a single local receiver moved by a device transition."""
        try:
            sinks = [
                sink
                for sink in flow.pulse_objects("sinks")
                if sink.get("name") == get_config().displayport_sink
                and pulse_index(sink.get("index"))
            ]
            streams = [
                stream
                for stream in flow.pulse_objects("sink-inputs")
                if isinstance(stream.get("properties"), dict)
                and stream["properties"].get("application.name") == "Shairport Sync"
                and stream["properties"].get("application.process.binary")
                == "shairport-sync"
            ]
        except ERRORS:
            return None  # Active playback probe reports unavailable audio.
        if len(sinks) != 1 or len(streams) != 1:
            return None  # Never guess at a missing or duplicated receiver.
        if not pulse_index(streams[0].get("index")) or not pulse_index(
            streams[0].get("sink")
        ):
            return None
        if streams[0]["sink"] != sinks[0]["index"]:
            return Finding(
                "receiver_misrouted",
                "Local Shairport stream left the configured PC sink after "
                "an audio-device transition",
            )
        return None

    def source_route(self) -> Finding | None:
        """Check the actual Pulse sink, including while Spotify is paused."""
        config = get_config()
        try:
            all_sinks = flow.pulse_objects("sinks")
            sinks = [
                item for item in all_sinks if item.get("name") == config.bridge_sink
            ]
            if len(sinks) != 1 or not pulse_index(sinks[0].get("index")):
                return Finding(
                    "host_unavailable",
                    "Private bridge sink is missing or ambiguous; source was not moved",
                )
            streams = self.soloist_streams()
        except ERRORS:
            return Finding("host_unavailable", "Cannot inspect the Spotify audio route")
        if not streams:
            return None
        if len(streams) != 1 or any(
            not pulse_index(item.get("index")) or not pulse_index(item.get("sink"))
            for item in streams
        ):
            return Finding(
                "unknown", "Soloist audio route is ambiguous; source was not moved"
            )
        if streams[0]["sink"] != sinks[0]["index"]:
            actual = next(
                (
                    str(item.get("name"))
                    for item in all_sinks
                    if item.get("index") == streams[0]["sink"]
                ),
                "unknown sink",
            )
            return Finding(
                "source_misrouted",
                f"Soloist is on {actual!r} instead of {config.bridge_sink!r}; "
                "checking before a targeted move",
            )
        return None

    def source_guard(self) -> None:
        config = get_config()
        for ip, follower in [
            (config.kitchen_ip, False),
            *[(ip, True) for ip, _ in config.followers],
        ]:
            status = bridge.wiim_request(ip, "getPlayerStatus")
            if status.get("status") in ("stop", "pause"):
                continue
            if status.get("status") == "play" and str(status.get("mode")) == (
                "99" if follower else "1"
            ):
                continue
            raise bridge.BridgeError(
                "WiiM is using another source or its playback state is unknown; not taking it over"
            )

    def reconcile(self, playing: bool, now: float, mode: str) -> bool:
        if mode == "auto":
            # A sleeping/unplugged DisplayPort device is a host condition, not
            # a failed receiver. Avoid connecting the local AirPlay path to a
            # fallback sink while the intended PC output is absent.
            if playing and not self.pc_sink_available():
                return False
            if playing and not flow.configured_outputs_selected():
                self.source_guard()
            return self.outputs.observe(playing, now)
        if mode == "manual":
            return False
        with output_control.locked():
            if output_control.mode() != mode:
                return False
            items = bridge.outputs()
            desired = [bridge.resolve_target(items, "local")] if mode == "local" else []
            selected_ids = {
                bridge.output_id(item) for item in items if item.get("selected") is True
            }
            desired_ids = {bridge.output_id(item) for item in desired}
            changed = selected_ids != desired_ids
            if changed:
                bridge.set_outputs(desired)
            if mode == "local" and playing:
                changed = bridge.resume_pcm_playback() or changed
            return changed

    def probe(self, mode: str) -> Finding:
        if not self.pc_sink_available():
            return Finding(
                "host_unavailable",
                "PC output sink is unavailable; waiting without receiver restarts",
            )
        receiver_state = flow.local_receiver_state()
        if receiver_state == "route":
            return Finding(
                "receiver_route", "Local receiver is missing, duplicated, or misrouted"
            )
        if receiver_state == "muted":
            return Finding(
                "receiver_muted",
                "Local receiver is muted; PC audio cannot be verified. "
                "Leaving the mute in place because it may be an intentional safety stop",
            )
        if receiver_state == "corked":
            return Finding(
                "receiver_corked",
                "Local receiver remains corked during active playback; "
                "PC audio cannot be verified",
            )
        if mode == "auto":
            try:
                bridge.validate_wiim_group()
                self.source_guard()
                status = bridge.wiim_request(get_config().kitchen_ip, "getPlayerStatus")
                if status.get("status") != "play" or str(status.get("mode")) != "1":
                    return Finding(
                        "wiim_disconnected",
                        "WiiM does not confirm active AirPlay playback",
                    )
            except ERRORS as exc:
                return Finding("wiim_unavailable", str(exc))
        config = get_config()
        source = config.bridge_sink + ".monitor"
        target = config.displayport_sink + ".monitor"
        peaks = flow.sample_peaks((source, target))
        if not self.playing():
            return Finding("idle", "Spotify stopped during the audio probe")
        if peaks[source] < flow.ACTIVE_PEAK:
            return Finding(
                "unverified",
                "Spotify is active but source PCM is silent; not evidence of a fault",
            )
        if peaks[target] < flow.ACTIVE_PEAK:
            return Finding(
                "downstream_silent", "Source PCM is present but the PC output is silent"
            )
        return Finding(
            "verified", "PCM reaches the PC; WiiM transport checked when in group mode"
        )

    def repair(self, finding: Finding, stage: int) -> str:
        if finding.kind == "receiver_misrouted":
            if self.receiver_route() is None:
                return "Local receiver route changed; no stream was moved"
            streams = [
                stream
                for stream in flow.pulse_objects("sink-inputs")
                if isinstance(stream.get("properties"), dict)
                and stream["properties"].get("application.name") == "Shairport Sync"
                and stream["properties"].get("application.process.binary")
                == "shairport-sync"
            ]
            if len(streams) != 1 or not pulse_index(streams[0].get("index")):
                return "Local receiver disappeared; no stream was moved"
            command(
                [
                    "/usr/bin/pactl",
                    "move-sink-input",
                    str(streams[0]["index"]),
                    get_config().displayport_sink,
                ],
                timeout=5,
            )
            if self.receiver_route() is not None:
                raise RuntimeError(
                    "Local receiver remains outside the configured PC sink"
                )
            return "Moved only the local receiver to the configured PC sink"
        if finding.kind == "capture_misrouted":
            current = self.capture_route()
            if current is None:
                return "PCM capture route recovered before intervention"
            if current.kind != "capture_misrouted":
                return "PCM capture route changed; no recorder was moved"
            streams = self.capture_streams()
            if len(streams) != 1 or not pulse_index(streams[0].get("index")):
                return "PCM capture disappeared; no recorder was moved"
            stream = streams[0]
            command(
                [
                    "/usr/bin/pactl",
                    "move-source-output",
                    str(stream["index"]),
                    get_config().bridge_sink + ".monitor",
                ],
                timeout=5,
            )
            if self.capture_route() is not None:
                raise RuntimeError("PCM capture remains outside the private bridge")
            return "Moved only the bridge PCM capture to its private monitor"
        if finding.kind == "source_misrouted":
            # Re-observe immediately before the move. A vanished stream or
            # sink must not turn into a broad container or host-audio restart.
            current_route = self.source_route()
            if current_route is None:
                return "Soloist route recovered before intervention"
            if current_route.kind != "source_misrouted":
                return "Soloist route changed; no stream was moved"
            stream = self.soloist_streams()[0]
            command(
                [
                    "/usr/bin/pactl",
                    "move-sink-input",
                    str(stream["index"]),
                    get_config().bridge_sink,
                ],
                timeout=5,
            )
            if self.source_route() is not None:
                raise RuntimeError("Soloist route remained outside the private sink")
            return "Moved Soloist stream back to the private bridge sink"
        if finding.kind == "wiim_disconnected":
            with output_control.locked():
                if not output_control.automatic_enabled() or not self.playing():
                    return "Reconnection cancelled: playback or manual intent changed"
                self.source_guard()
                bridge.reconcile_once()
                bridge.resume_pcm_playback()
            return "Reconnected stale WiiM AirPlay session"
        if finding.kind in ("stale_mount", "container_missing"):
            services = finding.services
            argv = [
                "/usr/bin/docker",
                "compose",
                "up",
                "-d",
                "--no-deps",
                *(["--force-recreate"] if finding.kind == "stale_mount" else []),
                *services,
            ]
        else:
            if finding.services:
                services = finding.services
            elif finding.kind in ("receiver_route", "downstream_silent"):
                services = ("shairport",) if stage == 0 else ("owntone", "capture")
            else:
                raise RuntimeError(
                    "no automatic destructive recovery for " + finding.kind
                )
            argv = [
                "/usr/bin/docker",
                "restart",
                "--time",
                "5",
                *["wiim-pc-bridge-" + service for service in services],
            ]
        if not services or not set(services).issubset(SERVICES):
            raise RuntimeError("refusing recovery of an unrecognized service")
        command(argv, timeout=60)
        return (
            "Recreated "
            if finding.kind in ("stale_mount", "container_missing")
            else "Restarted "
        ) + ", ".join(services)


@dataclass
class Supervisor:
    runtime: Runtime
    started: float
    state: str = "starting"
    detail: str = "Waiting for the first observation"
    next_infrastructure: float = 0
    next_probe: float = 0
    settle_until: float = 0
    previous_fault: str = ""
    consecutive: int = 0
    stage: int = 0
    repair_times: list[float] = field(default_factory=list)
    route_repair_times: list[float] = field(default_factory=list)
    repairs: int = 0
    route_repairs: int = 0
    source_interruptions: int = 0
    source_identity: str | None = None
    muted_desktop_stream: int | None = None
    verified_samples: int = 0
    playback_sessions: int = 0
    active_seconds: float = 0
    last_tick: float | None = None
    was_playing: bool = False
    last_action: str = "none"
    infrastructure_fault: Finding | None = None

    def restore(self, now: float) -> None:
        """Keep recovery budgets and soak counters across monitor-only restarts."""
        try:
            data = json.loads(status_path().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("supervisor status is not an object")
            for key in (
                "repairs",
                "route_repairs",
                "source_interruptions",
                "verified_samples",
                "playback_sessions",
            ):
                value = data.get(key, 0)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    setattr(self, key, value)
            seconds = float(data.get("active_seconds", 0))
            if math.isfinite(seconds):
                self.active_seconds = max(0, seconds)
            wall = time.time()
            self.repair_times = [
                now - (wall - float(stamp))
                for stamp in data.get("repair_timestamps", [])
                if 0 <= wall - float(stamp) < 600
            ]
            self.route_repair_times = [
                now - (wall - float(stamp))
                for stamp in data.get("route_repair_timestamps", [])
                if 0 <= wall - float(stamp) < 600
            ]
            if self.repair_times:
                self.settle_until = max(self.repair_times) + 60
                self.next_probe = self.settle_until
            self.last_action = str(data.get("last_action", "none"))
            identity = data.get("source_identity")
            if isinstance(identity, str):
                self.source_identity = identity
            muted_stream = data.get("muted_desktop_stream")
            if pulse_index(muted_stream):
                self.muted_desktop_stream = muted_stream
            if 0 <= wall - float(data["updated_at"]) < 120:
                self.was_playing = data.get("was_playing") is True
                self.last_tick = now
        except (OSError, ValueError, TypeError, KeyError):
            logging.warning(
                "No valid previous supervisor counters; starting a new observation period"
            )

    def transition(self, state: str, detail: str) -> None:
        if (state, detail) != (self.state, self.detail):
            logging.info("%s: %s", state, detail)
        self.state, self.detail = state, detail

    def guard_desktop_audio(self, playing: bool, now: float) -> None:
        """Keep the desktop app out of capture and off conflicting PC audio."""
        try:
            streams = self.runtime.desktop_spotify_streams()
            in_bridge = (
                self.runtime.desktop_spotify_in_bridge(streams[0]["index"])
                if len(streams) == 1
                else False
            )
            conflict = playing or in_bridge
            if self.muted_desktop_stream is not None:
                owned = next(
                    (
                        item
                        for item in streams
                        if item["index"] == self.muted_desktop_stream
                    ),
                    None,
                )
                if owned is None:
                    # PipeWire may recreate the Flatpak stream and carry our
                    # mute into its stream-restore state. Keep the ownership
                    # claim until the sole replacement appears; otherwise the
                    # inherited mute can strand direct PC playback silently.
                    if len(streams) != 1:
                        return
                    owned = streams[0]
                    if owned["mute"]:
                        self.muted_desktop_stream = owned["index"]
                        self.persist(now)
                    else:
                        self.muted_desktop_stream = None
                if self.muted_desktop_stream is not None:
                    if conflict and not owned["mute"]:
                        self.runtime.set_desktop_spotify_mute(
                            self.muted_desktop_stream, True
                        )
                    elif not conflict and self.runtime.set_desktop_spotify_mute(
                        self.muted_desktop_stream, False
                    ):
                        logging.info(
                            "Restored desktop Spotify audio after conflict ended"
                        )
                        self.muted_desktop_stream = None
                    return
            if not conflict or len(streams) != 1 or streams[0]["mute"]:
                return
            index = streams[0]["index"]
            # Persist ownership before the mute so a supervisor restart can
            # safely undo a mute that completed just before a crash.
            self.muted_desktop_stream = index
            self.persist(now)
            if self.runtime.set_desktop_spotify_mute(index, True):
                logging.info("Muted desktop Spotify stream during bridge conflict")
        except ERRORS as exc:
            logging.warning("Could not reconcile desktop Spotify audio: %s", exc)

    def fault(self, finding: Finding, now: float) -> None:
        self.transition("degraded", finding.detail)
        if finding.kind in (
            "host_unavailable",
            "wiim_unavailable",
            "receiver_muted",
            "receiver_corked",
            "capture_unknown",
            "unknown",
        ):
            self.previous_fault, self.consecutive = "", 0
            return
        self.consecutive = (
            self.consecutive + 1 if self.previous_fault == finding.kind else 1
        )
        self.previous_fault = finding.kind
        is_route = finding.kind in (
            "source_misrouted",
            "capture_misrouted",
            "receiver_misrouted",
        )
        # A misrouted capture can immediately create an audio feedback loop.
        # The repair rechecks exact identity, so do not wait for a second tick.
        required = 1 if finding.kind == "capture_misrouted" else 2
        if self.consecutive < required or (now < self.settle_until and not is_route):
            return
        budget = self.route_repair_times if is_route else self.repair_times
        budget[:] = [attempt for attempt in budget if now - attempt < 600]
        if len(budget) >= ROUTE_REPAIR_LIMIT:
            self.transition(
                "degraded",
                finding.detail
                + "; recovery budget exhausted, waiting before another attempt",
            )
            return
        budget.append(now)
        self.repairs += 1
        if is_route:
            self.route_repairs += 1
        self.transition("recovering", finding.detail)
        self.persist(now)
        try:
            self.last_action = self.runtime.repair(finding, self.stage)
        finally:
            self.stage = 0 if is_route else self.stage + 1
            self.consecutive = 0
            self.settle_until = now + (SETTLE_SECONDS if is_route else 60)
            self.next_infrastructure = 0
        self.transition("recovering", self.last_action)

    def tick(self, now: float) -> None:
        mode = output_control.mode()
        if now >= self.next_infrastructure:
            self.infrastructure_fault = self.runtime.infrastructure()
            identity = self.runtime.source_identity
            if identity is not None:
                if (
                    self.source_identity is not None
                    and identity != self.source_identity
                ):
                    self.source_interruptions += 1
                    logging.warning("Spotify source process restarted: %s", identity)
                self.source_identity = identity
            self.next_infrastructure = now + PROBE_PERIOD
            if self.infrastructure_fault:
                self.fault(self.infrastructure_fault, now)
                return
        if self.infrastructure_fault:
            return
        playing = self.runtime.playing()
        self.guard_desktop_audio(playing, now)
        # A bad Soloist route must not prevent restoring our desktop-app mute
        # once bridge playback has stopped.
        capture_fault = self.runtime.capture_route()
        if capture_fault is not None:
            self.fault(capture_fault, now)
            return
        route_fault = self.runtime.source_route()
        if route_fault is not None:
            self.fault(route_fault, now)
            return
        receiver_fault = self.runtime.receiver_route()
        if receiver_fault is not None:
            self.fault(receiver_fault, now)
            return
        if playing:
            if not self.was_playing:
                self.playback_sessions += 1
            if self.was_playing and self.last_tick is not None:
                self.active_seconds += min(PERIOD * 2, max(0, now - self.last_tick))
        self.was_playing = playing
        self.last_tick = now
        if self.runtime.reconcile(playing, now, mode):
            self.settle_until = now + SETTLE_SECONDS
            self.consecutive = 0
            self.next_probe = self.settle_until
        if mode in ("stopped", "manual"):
            self.transition(
                "manual", "Automatic playback disabled by explicit user command"
            )
            return
        if not playing:
            self.previous_fault, self.consecutive, self.stage = "", 0, 0
            self.transition(
                "idle", "Spotify is idle; no automatic connection or signal injection"
            )
            return
        if now < self.settle_until:
            self.transition("connecting", "Waiting for receiver buffers to settle")
            return
        if now < self.next_probe:
            return
        self.next_probe = now + PROBE_PERIOD
        finding = self.runtime.probe(mode)
        if finding.kind == "verified":
            self.verified_samples += 1
            self.previous_fault, self.consecutive, self.stage = "", 0, 0
            self.transition("playing", finding.detail)
        elif finding.kind in ("idle", "unverified"):
            self.previous_fault, self.consecutive = "", 0
            self.transition(finding.kind, finding.detail)
        else:
            self.fault(finding, now)

    def persist(self, now: float) -> None:
        write_status(
            {
                "version": 1,
                "updated_at": time.time(),
                "pid": os.getpid(),
                "uptime_seconds": round(now - self.started, 1),
                "state": self.state,
                "detail": self.detail,
                "last_action": self.last_action,
                "repairs": self.repairs,
                "route_repairs": self.route_repairs,
                "source_interruptions": self.source_interruptions,
                "source_identity": self.source_identity,
                "muted_desktop_stream": self.muted_desktop_stream,
                "verified_samples": self.verified_samples,
                "playback_sessions": self.playback_sessions,
                "active_seconds": round(self.active_seconds, 1),
                "was_playing": self.was_playing,
                "repair_timestamps": [
                    time.time() - (now - stamp) for stamp in self.repair_times
                ],
                "route_repair_timestamps": [
                    time.time() - (now - stamp) for stamp in self.route_repair_times
                ],
            }
        )


def request_stop(_signum: int, _frame: object) -> None:
    global stopping
    stopping = True


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log_path = PROJECT / "runtime/supervisor-events.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=512_000, backupCount=3
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )
    supervisor = Supervisor(Runtime(), time.monotonic())
    supervisor.restore(time.monotonic())
    supervisor.persist(time.monotonic())
    notify("READY=1")
    while not stopping:
        now = time.monotonic()
        try:
            supervisor.tick(now)
        except ERRORS as exc:
            supervisor.transition(
                "degraded", "Observation/recovery unavailable: " + str(exc)
            )
        supervisor.persist(time.monotonic())
        notify("WATCHDOG=1\nSTATUS=" + supervisor.state + ": " + supervisor.detail)
        for _ in range(int(PERIOD * 4)):
            if stopping:
                break
            time.sleep(0.25)
    supervisor.transition(
        "stopped", "Supervisor stopped; container lifetimes are unchanged"
    )
    supervisor.persist(time.monotonic())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
