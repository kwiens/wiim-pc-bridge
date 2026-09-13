"""Shared, dependency-free configuration for the bridge utilities."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
KNOWN_KEYS = frozenset(
    {
        "BRIDGE_FRIENDLY_NAME",
        "BRIDGE_GID",
        "BRIDGE_RUNTIME_DIR",
        "BRIDGE_SINK",
        "BRIDGE_UID",
        "DISPLAYPORT_SINK",
        "KITCHEN_DEVICE_NAME",
        "KITCHEN_IP",
        "LIVING_ROOM_DEVICE_NAME",
        "LIVING_ROOM_IP",
        "LOCAL_OFFSET_MS",
        "LOCAL_OUTPUT_NAME",
        "LOCAL_VOLUME",
        "PULSE_COOKIE_PATH",
        "SOLOIST_KEY_FILE",
        "START_BUFFER_MS",
        "TRUSTED_NETWORK",
        "WIIM_OFFSET_MS",
        "WIIM_OUTPUT_NAME",
        "WIIM_VOLUME",
    }
)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid configuration at {path}:{line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid configuration key at {path}:{line_number}")
        if key not in KNOWN_KEYS:
            raise ValueError(
                f"unknown configuration key at {path}:{line_number}: {key}"
            )
        if key in values:
            raise ValueError(
                f"duplicate configuration key at {path}:{line_number}: {key}"
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _integer(values: dict[str, str], key: str, default: int) -> int:
    raw = os.environ.get(key, values.get(key, str(default)))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _value(values: dict[str, str], key: str, default: str) -> str:
    return os.environ.get(key, values.get(key, default))


@dataclass(frozen=True)
class BridgeConfig:
    uid: int
    gid: int
    runtime_dir: Path
    soloist_key_file: Path
    pulse_cookie_path: Path
    displayport_sink: str
    bridge_sink: str
    friendly_name: str
    kitchen_ip: str
    living_room_ip: str
    kitchen_device_name: str
    living_room_device_name: str
    wiim_output_name: str
    local_output_name: str
    trusted_network: str
    local_volume: int
    wiim_volume: int
    local_offset_ms: int
    wiim_offset_ms: int
    start_buffer_ms: int


def load_config() -> BridgeConfig:
    env_path = Path(os.environ.get("WIIM_BRIDGE_ENV", PROJECT / ".env"))
    values = _read_env_file(env_path)
    uid = _integer(values, "BRIDGE_UID", os.getuid())
    config = BridgeConfig(
        uid=uid,
        gid=_integer(values, "BRIDGE_GID", os.getgid()),
        runtime_dir=Path(_value(values, "BRIDGE_RUNTIME_DIR", f"/run/user/{uid}")),
        soloist_key_file=Path(
            _value(
                values,
                "SOLOIST_KEY_FILE",
                str(Path.home() / ".config/wiim-pc-bridge/soloist_api_key"),
            )
        ),
        pulse_cookie_path=Path(
            _value(
                values,
                "PULSE_COOKIE_PATH",
                str(Path.home() / ".config/pulse/cookie"),
            )
        ),
        displayport_sink=_value(values, "DISPLAYPORT_SINK", ""),
        bridge_sink=_value(values, "BRIDGE_SINK", "wiim_bridge"),
        friendly_name=_value(values, "BRIDGE_FRIENDLY_NAME", "PC + WiiM"),
        kitchen_ip=_value(values, "KITCHEN_IP", "192.0.2.10"),
        living_room_ip=_value(values, "LIVING_ROOM_IP", "192.0.2.11"),
        kitchen_device_name=_value(values, "KITCHEN_DEVICE_NAME", "Kitchen"),
        living_room_device_name=_value(
            values, "LIVING_ROOM_DEVICE_NAME", "Living Room"
        ),
        wiim_output_name=_value(
            values, "WIIM_OUTPUT_NAME", "WiiM group (Kitchen leader)"
        ),
        local_output_name=_value(values, "LOCAL_OUTPUT_NAME", "This PC DisplayPort"),
        trusted_network=_value(values, "TRUSTED_NETWORK", "192.0.2.0/24"),
        local_volume=_integer(values, "LOCAL_VOLUME", 100),
        wiim_volume=_integer(values, "WIIM_VOLUME", 50),
        local_offset_ms=_integer(values, "LOCAL_OFFSET_MS", 0),
        wiim_offset_ms=_integer(values, "WIIM_OFFSET_MS", 0),
        start_buffer_ms=_integer(values, "START_BUFFER_MS", 2250),
    )
    for name, volume in (
        ("LOCAL_VOLUME", config.local_volume),
        ("WIIM_VOLUME", config.wiim_volume),
    ):
        if not 0 <= volume <= 100:
            raise ValueError(f"{name} must be between 0 and 100")
    for name, offset in (
        ("LOCAL_OFFSET_MS", config.local_offset_ms),
        ("WIIM_OFFSET_MS", config.wiim_offset_ms),
    ):
        if not -2000 <= offset <= 2000:
            raise ValueError(f"{name} must be between -2000 and 2000")
    if config.uid < 0 or config.gid < 0:
        raise ValueError("BRIDGE_UID and BRIDGE_GID must be non-negative")
    for name, path in (
        ("BRIDGE_RUNTIME_DIR", config.runtime_dir),
        ("SOLOIST_KEY_FILE", config.soloist_key_file),
        ("PULSE_COOKIE_PATH", config.pulse_cookie_path),
    ):
        if not path.is_absolute():
            raise ValueError(f"{name} must be an absolute path")
    for name, value in (
        ("DISPLAYPORT_SINK", config.displayport_sink),
        ("BRIDGE_SINK", config.bridge_sink),
        ("BRIDGE_FRIENDLY_NAME", config.friendly_name),
        ("KITCHEN_DEVICE_NAME", config.kitchen_device_name),
        ("LIVING_ROOM_DEVICE_NAME", config.living_room_device_name),
        ("WIIM_OUTPUT_NAME", config.wiim_output_name),
        ("LOCAL_OUTPUT_NAME", config.local_output_name),
    ):
        if not value:
            raise ValueError(f"{name} must not be empty")
    for name, value in (
        ("DISPLAYPORT_SINK", config.displayport_sink),
        ("BRIDGE_SINK", config.bridge_sink),
    ):
        if any(character.isspace() for character in value):
            raise ValueError(f"{name} must not contain whitespace")
    try:
        kitchen = ipaddress.ip_address(config.kitchen_ip)
        living_room = ipaddress.ip_address(config.living_room_ip)
        trusted_network = ipaddress.ip_network(config.trusted_network, strict=False)
    except ValueError as exc:
        raise ValueError(
            "WiiM addresses and TRUSTED_NETWORK must be valid IP values"
        ) from exc
    if kitchen.version != 4 or living_room.version != 4:
        raise ValueError("WiiM addresses must currently be IPv4")
    if kitchen == living_room:
        raise ValueError("KITCHEN_IP and LIVING_ROOM_IP must be different")
    if trusted_network.version != 4:
        raise ValueError("TRUSTED_NETWORK must currently be IPv4")
    if kitchen not in trusted_network or living_room not in trusted_network:
        raise ValueError("both WiiM addresses must be inside TRUSTED_NETWORK")
    if not 250 <= config.start_buffer_ms <= 10000:
        raise ValueError("START_BUFFER_MS must be between 250 and 10000")
    return config


CONFIG = load_config()
