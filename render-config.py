#!/usr/bin/env python3
"""Render runtime configuration from the local .env file."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys

from bridge_config import PROJECT, ConfigError, get_config

PLACEHOLDER = re.compile(r"@([A-Z0-9_]+)@")

# Values the POSIX shell helpers need. Exporting them from here keeps a single
# .env parser: the shell no longer sources the file and so cannot disagree with
# the Python tools about comments, quoting, or expansion.
EXPORTED_KEYS = (
    "BRIDGE_UID",
    "BRIDGE_GID",
    "BRIDGE_RUNTIME_DIR",
    "SOLOIST_KEY_FILE",
    "PULSE_COOKIE_PATH",
    "DISPLAYPORT_SINK",
    "BRIDGE_SINK",
)


def owntone_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render(template: str, replacements: dict[str, str]) -> str:
    """Substitute every placeholder in one pass.

    A single pass matters: with sequential str.replace calls a value inserted
    early is still a substitution target for later placeholders, so a config
    value containing @LOCAL_OUTPUT_NAME@ would silently expand.
    """
    present = {match.group(1) for match in PLACEHOLDER.finditer(template)}
    unknown = present.difference(replacements)
    if unknown:
        raise RuntimeError(
            "unresolved placeholder in OwnTone configuration: "
            + ", ".join(sorted(unknown))
        )
    return PLACEHOLDER.sub(lambda match: replacements[match.group(1)], template)


def replacements() -> dict[str, str]:
    config = get_config()
    return {
        # OwnTone matches trusted networks on dotted-octet prefixes, not CIDR,
        # so the configured network is converted to the form it understands.
        "TRUSTED_NETWORK": owntone_escape(config.trusted_network_prefix),
        "START_BUFFER_MS": str(config.start_buffer_ms),
        "BRIDGE_FRIENDLY_NAME": owntone_escape(config.friendly_name),
        "KITCHEN_DEVICE_NAME": owntone_escape(config.kitchen_device_name),
        "WIIM_OUTPUT_NAME": owntone_escape(config.wiim_output_name),
        "LOCAL_OUTPUT_NAME": owntone_escape(config.local_output_name),
    }


def write_runtime_config() -> None:
    template = (PROJECT / "owntone.conf.in").read_text(encoding="utf-8")
    rendered = render(template, replacements())

    runtime_directory = PROJECT / "runtime"
    runtime_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = runtime_directory / "owntone.conf"

    # Write through the existing inode instead of renaming a temporary over it.
    # compose.yaml bind-mounts this single file, and a rename swaps the inode:
    # a running OwnTone container would keep the deleted original and never see
    # a re-rendered configuration.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())
    target.chmod(0o600)


def export_shell() -> None:
    config = get_config()
    fields = {
        "BRIDGE_UID": str(config.uid),
        "BRIDGE_GID": str(config.gid),
        "BRIDGE_RUNTIME_DIR": str(config.runtime_dir),
        "SOLOIST_KEY_FILE": str(config.soloist_key_file),
        "PULSE_COOKIE_PATH": str(config.pulse_cookie_path),
        "DISPLAYPORT_SINK": config.displayport_sink,
        "BRIDGE_SINK": config.bridge_sink,
    }
    for key in EXPORTED_KEYS:
        print(f"{key}={shlex.quote(fields[key])}")


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--export",
        action="store_true",
        help="print shell-quoted assignments instead of rendering owntone.conf",
    )
    args = cli.parse_args()
    try:
        if args.export:
            export_shell()
        else:
            write_runtime_config()
    except ConfigError as exc:
        print(f"error: invalid configuration: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
