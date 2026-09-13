#!/usr/bin/env python3
"""Render runtime configuration from the local .env file."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from bridge_config import CONFIG, PROJECT


def owntone_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> int:
    template = (PROJECT / "owntone.conf.in").read_text(encoding="utf-8")
    replacements = {
        "@TRUSTED_NETWORK@": owntone_escape(CONFIG.trusted_network),
        "@START_BUFFER_MS@": str(CONFIG.start_buffer_ms),
        "@BRIDGE_FRIENDLY_NAME@": owntone_escape(CONFIG.friendly_name),
        "@KITCHEN_DEVICE_NAME@": owntone_escape(CONFIG.kitchen_device_name),
        "@WIIM_OUTPUT_NAME@": owntone_escape(CONFIG.wiim_output_name),
        "@LOCAL_OUTPUT_NAME@": owntone_escape(CONFIG.local_output_name),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    if re.search(r"@[A-Z0-9_]+@", rendered):
        raise RuntimeError("unresolved placeholder in OwnTone configuration")

    runtime_directory = PROJECT / "runtime"
    runtime_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".owntone.conf.", dir=runtime_directory, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
        temporary.chmod(0o600)
        temporary.replace(runtime_directory / "owntone.conf")
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
