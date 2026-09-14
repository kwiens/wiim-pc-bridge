"""Test fixtures that isolate the suite from the developer's own machine."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import bridge_config

# Deliberately distinct from every default and from each other: equal values
# let an argument swap pass unnoticed.
BASE_ENV = {
    "BRIDGE_UID": "1000",
    "BRIDGE_GID": "1000",
    "BRIDGE_RUNTIME_DIR": "/run/user/1000",
    "SOLOIST_KEY_FILE": "/home/tester/.config/wiim-pc-bridge/soloist_api_key",
    "PULSE_COOKIE_PATH": "/home/tester/.config/pulse/cookie",
    "DISPLAYPORT_SINK": "alsa_output.test-card",
    "BRIDGE_SINK": "test_bridge_sink",
    "BRIDGE_FRIENDLY_NAME": "Test Bridge",
    "KITCHEN_IP": "192.0.2.10",
    "LIVING_ROOM_IP": "192.0.2.11",
    "KITCHEN_DEVICE_NAME": "Kitchen",
    "LIVING_ROOM_DEVICE_NAME": "Living Room",
    "WIIM_OUTPUT_NAME": "Test WiiM Group",
    "LOCAL_OUTPUT_NAME": "Test PC Output",
    "TRUSTED_NETWORK": "192.0.2.0/24",
    "LOCAL_VOLUME": "71",
    "WIIM_VOLUME": "39",
    "LOCAL_OFFSET_MS": "125",
    "WIIM_OFFSET_MS": "-80",
    "START_BUFFER_MS": "1750",
}


def render_env(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


@contextlib.contextmanager
def configured(text: str | None = None, **overrides: str | None) -> Iterator[Path]:
    """Run a test against a temporary .env, ignoring the host's environment.

    `_value` and `_integer` consult os.environ before the file, so any of the
    known keys exported in a developer's shell would otherwise silently win
    over the fixture and fail unrelated assertions.
    """
    if text is None:
        values = dict(BASE_ENV)
        for key, value in overrides.items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        text = render_env(values)
    elif overrides:
        raise TypeError("pass either raw text or keyword overrides, not both")

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ".env"
        path.write_text(text, encoding="utf-8")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in bridge_config.KNOWN_KEYS
        }
        environment["WIIM_BRIDGE_ENV"] = str(path)
        with mock.patch.dict(os.environ, environment, clear=True):
            bridge_config.get_config.cache_clear()
            try:
                yield path
            finally:
                bridge_config.get_config.cache_clear()
