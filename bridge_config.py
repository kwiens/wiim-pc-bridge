"""Shared, dependency-free configuration for the bridge utilities."""

from __future__ import annotations

import difflib
import functools
import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
INLINE_COMMENT = re.compile(r"\s#")

VOLUME_RANGE = (0, 100)
OFFSET_RANGE_MS = (-2000, 2000)
START_BUFFER_RANGE_MS = (250, 10000)

# OwnTone matches trusted_networks on dotted-octet prefixes, so only
# octet-aligned CIDR prefixes can be rendered without widening the network.
OWNTONE_PREFIX_LENGTHS = (8, 16, 24)

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
        "KEEPALIVE_ENABLED",
        "KEEPALIVE_INTERVAL_SECONDS",
        "KEEPALIVE_DURATION_SECONDS",
        "KEEPALIVE_LEVEL_DB",
        "LIVING_ROOM_DEVICE_NAME",
        "LIVING_ROOM_IP",
        "LOCAL_OFFSET_MS",
        "LOCAL_OUTPUT_NAME",
        "LOCAL_VOLUME",
        "PULSE_COOKIE_PATH",
        "SOLOIST_KEY_FILE",
        "START_BUFFER_MS",
        "TRUSTED_NETWORK",
        "WIIM_FOLLOWERS",
        "WIIM_OFFSET_MS",
        "WIIM_OUTPUT_NAME",
        "WIIM_VOLUME",
    }
)

# Docker Compose reads these from the same .env file. They are not bridge
# settings, so this parser ignores them instead of failing the whole tool.
COMPOSE_KEYS = frozenset(
    {
        "COMPOSE_ENV_FILES",
        "COMPOSE_FILE",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
    }
)


class ConfigError(ValueError):
    """Raised for any problem with the local .env file."""


def _strip_value(raw: str, where: str) -> str:
    """Apply the same quoting and comment rules Docker Compose uses."""
    value = raw.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        if end < 0:
            raise ConfigError(f"unterminated quote at {where}")
        inner = value[1:end]
        trailing = value[end + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            raise ConfigError(f"unexpected text after the quoted value at {where}")
        return inner
    match = INLINE_COMMENT.search(value)
    if match:
        value = value[: match.start()]
    return value.strip()


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        where = f"{path}:{line_number}"
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"invalid configuration at {where}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ConfigError(f"invalid configuration key at {where}")
        if key in values:
            raise ConfigError(f"duplicate configuration key at {where}: {key}")
        if key in COMPOSE_KEYS:
            continue
        if key not in KNOWN_KEYS:
            hint = difflib.get_close_matches(key, sorted(KNOWN_KEYS), n=1)
            suggestion = f"; did you mean {hint[0]}?" if hint else ""
            raise ConfigError(
                f"unknown configuration key at {where}: {key}{suggestion}"
            )
        value = _strip_value(raw_value, where)
        # Docker Compose interpolates $VAR in this file while this parser does
        # not, so a literal $ would mean two different things to two readers.
        if "$" in value:
            raise ConfigError(
                f"{key} at {where} must not contain '$'; Docker Compose would "
                "expand it and the bridge tools would not"
            )
        values[key] = value
    return values


def _integer(values: dict[str, str], key: str, default: int) -> int:
    raw = os.environ.get(key, values.get(key, str(default)))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _value(values: dict[str, str], key: str, default: str) -> str:
    return os.environ.get(key, values.get(key, default))


def _bounded(name: str, value: int, bounds: tuple[int, int]) -> None:
    low, high = bounds
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")


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
    followers: tuple[tuple[str, str], ...]
    local_volume: int
    wiim_volume: int
    local_offset_ms: int
    wiim_offset_ms: int
    start_buffer_ms: int
    keepalive_enabled: bool
    keepalive_interval_seconds: int
    keepalive_duration_seconds: int
    keepalive_level_db: int

    @property
    def trusted_network_prefix(self) -> str:
        """The dotted-octet prefix OwnTone's trusted_networks actually matches."""
        network = ipaddress.ip_network(self.trusted_network, strict=True)
        octets = network.prefixlen // 8
        return ".".join(str(network.network_address).split(".")[:octets])

    @property
    def follower_ips(self) -> tuple[str, ...]:
        return tuple(ip for ip, _name in self.followers)


def _parse_followers(raw: str, default_ip: str, default_name: str) -> tuple:
    if not raw.strip():
        return ((default_ip, default_name),)
    followers: list[tuple[str, str]] = []
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if "=" not in item:
            raise ConfigError(
                f"WIIM_FOLLOWERS entry {item!r} must be written as ADDRESS=Name"
            )
        ip, name = item.split("=", 1)
        ip, name = ip.strip(), name.strip()
        if not ip or not name:
            raise ConfigError(
                f"WIIM_FOLLOWERS entry {item!r} must be written as ADDRESS=Name"
            )
        followers.append((ip, name))
    if not followers:
        raise ConfigError("WIIM_FOLLOWERS must list at least one follower")
    return tuple(followers)


def _validate_trusted_network(raw: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(raw, strict=True)
    except ValueError as exc:
        raise ConfigError(
            f"TRUSTED_NETWORK must be a CIDR network with no host bits set, got {raw!r}"
        ) from exc
    if network.version != 4:
        raise ConfigError("TRUSTED_NETWORK must currently be IPv4")
    if network.prefixlen not in OWNTONE_PREFIX_LENGTHS:
        raise ConfigError(
            "TRUSTED_NETWORK must use a /8, /16 or /24 prefix because OwnTone "
            "matches trusted networks on whole dotted octets"
        )
    if not network.is_private:
        raise ConfigError(
            "TRUSTED_NETWORK must be a private network; it grants unauthenticated "
            "OwnTone control to every address inside it"
        )
    return network


def _validate_device_address(
    name: str, raw: str, network: ipaddress.IPv4Network
) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a literal IP address, got {raw!r}") from exc
    if address.version != 4:
        raise ConfigError(f"{name} must currently be IPv4")
    if address not in network:
        raise ConfigError(f"{name} ({raw}) must be inside TRUSTED_NETWORK")
    return address


def load_config() -> BridgeConfig:
    env_path = Path(os.environ.get("WIIM_BRIDGE_ENV", PROJECT / ".env"))
    values = _read_env_file(env_path)
    uid = _integer(values, "BRIDGE_UID", os.getuid())
    living_room_ip = _value(values, "LIVING_ROOM_IP", "192.0.2.11")
    living_room_device_name = _value(values, "LIVING_ROOM_DEVICE_NAME", "Living Room")
    keepalive_enabled = _integer(values, "KEEPALIVE_ENABLED", 0)
    _bounded("KEEPALIVE_ENABLED", keepalive_enabled, (0, 1))
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
        living_room_ip=living_room_ip,
        kitchen_device_name=_value(values, "KITCHEN_DEVICE_NAME", "Kitchen"),
        living_room_device_name=living_room_device_name,
        wiim_output_name=_value(
            values, "WIIM_OUTPUT_NAME", "WiiM group (Kitchen leader)"
        ),
        local_output_name=_value(values, "LOCAL_OUTPUT_NAME", "This PC DisplayPort"),
        trusted_network=_value(values, "TRUSTED_NETWORK", "192.0.2.0/24"),
        followers=_parse_followers(
            _value(values, "WIIM_FOLLOWERS", ""),
            living_room_ip,
            living_room_device_name,
        ),
        local_volume=_integer(values, "LOCAL_VOLUME", 100),
        wiim_volume=_integer(values, "WIIM_VOLUME", 50),
        local_offset_ms=_integer(values, "LOCAL_OFFSET_MS", 0),
        wiim_offset_ms=_integer(values, "WIIM_OFFSET_MS", 0),
        start_buffer_ms=_integer(values, "START_BUFFER_MS", 2250),
        keepalive_enabled=bool(keepalive_enabled),
        keepalive_interval_seconds=_integer(values, "KEEPALIVE_INTERVAL_SECONDS", 600),
        keepalive_duration_seconds=_integer(values, "KEEPALIVE_DURATION_SECONDS", 3),
        keepalive_level_db=_integer(values, "KEEPALIVE_LEVEL_DB", -42),
    )
    _bounded("LOCAL_VOLUME", config.local_volume, VOLUME_RANGE)
    _bounded("WIIM_VOLUME", config.wiim_volume, VOLUME_RANGE)
    _bounded("LOCAL_OFFSET_MS", config.local_offset_ms, OFFSET_RANGE_MS)
    _bounded("WIIM_OFFSET_MS", config.wiim_offset_ms, OFFSET_RANGE_MS)
    _bounded("START_BUFFER_MS", config.start_buffer_ms, START_BUFFER_RANGE_MS)
    _bounded(
        "KEEPALIVE_INTERVAL_SECONDS", config.keepalive_interval_seconds, (60, 1800)
    )
    _bounded("KEEPALIVE_DURATION_SECONDS", config.keepalive_duration_seconds, (1, 10))
    _bounded("KEEPALIVE_LEVEL_DB", config.keepalive_level_db, (-60, -20))
    if config.uid < 0 or config.gid < 0:
        raise ConfigError("BRIDGE_UID and BRIDGE_GID must be non-negative")
    for name, path in (
        ("BRIDGE_RUNTIME_DIR", config.runtime_dir),
        ("SOLOIST_KEY_FILE", config.soloist_key_file),
        ("PULSE_COOKIE_PATH", config.pulse_cookie_path),
    ):
        if not path.is_absolute():
            raise ConfigError(f"{name} must be an absolute path")
    # A key inside the clone is one `git add -A` away from being published.
    if PROJECT in config.soloist_key_file.parents:
        raise ConfigError(
            "SOLOIST_KEY_FILE must live outside the repository so it cannot be "
            "committed; use a path such as ~/.config/wiim-pc-bridge/soloist_api_key"
        )
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
            raise ConfigError(f"{name} must not be empty")
    for name, value in (
        ("DISPLAYPORT_SINK", config.displayport_sink),
        ("BRIDGE_SINK", config.bridge_sink),
    ):
        if any(character.isspace() for character in value):
            raise ConfigError(f"{name} must not contain whitespace")
    network = _validate_trusted_network(config.trusted_network)
    leader = _validate_device_address("KITCHEN_IP", config.kitchen_ip, network)
    seen = {leader}
    for ip, follower_name in config.followers:
        address = _validate_device_address(f"follower {follower_name}", ip, network)
        if address in seen:
            raise ConfigError(f"duplicate WiiM address {ip}")
        seen.add(address)
    return config


@functools.lru_cache(maxsize=1)
def get_config() -> BridgeConfig:
    """Load and validate the configuration once, on first use."""
    return load_config()


class _LazyConfig:
    """Defer configuration loading until an attribute is actually read.

    Importing a module must not read or validate .env: `doctor.py` exists to
    diagnose a broken install, and the test suite must import cleanly on a
    fresh clone that has no .env at all.
    """

    def __getattr__(self, name: str) -> object:
        return getattr(get_config(), name)

    def __repr__(self) -> str:
        return repr(get_config())


CONFIG = _LazyConfig()
