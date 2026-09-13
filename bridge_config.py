"""Shared, dependency-free configuration for the bridge utilities."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


PROJECT = Path(__file__).resolve().parent


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
        runtime_dir=Path(
            _value(values, "BRIDGE_RUNTIME_DIR", f"/run/user/{uid}")
        ),
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
        local_output_name=_value(
            values, "LOCAL_OUTPUT_NAME", "This PC DisplayPort"
        ),
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
    return config


CONFIG = load_config()
