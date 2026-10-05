#!/usr/bin/env python3
"""Safe OwnTone output control for the PC + WiiM audio bridge."""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import output_control
from bridge_config import (
    OFFSET_RANGE_MS,
    VOLUME_RANGE,
    ConfigError,
    get_config,
)

API = "http://127.0.0.1:3689/api"
LOCAL_TYPE = "AirPlay 1"
WIIM_TYPE = "AirPlay 2"
TIMEOUT_SECONDS = 5

# Errors raised while reading a response body are not wrapped into URLError by
# urllib, so they have to be named explicitly or they escape as tracebacks.
TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    OSError,
)


class BridgeError(RuntimeError):
    pass


def target_output(target: str) -> tuple[str, str]:
    """Map a CLI target onto the OwnTone output name and type it selects."""
    config = get_config()
    if target == "local":
        return config.local_output_name, LOCAL_TYPE
    if target == "wiim":
        return config.wiim_output_name, WIIM_TYPE
    raise BridgeError(f"unknown target {target!r}")


def request(
    path: str, method: str = "GET", payload: object | None = None
) -> object | None:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        # API is a fixed loopback HTTP origin; caller controls only its path.
        with urllib.request.urlopen(  # nosec B310
            req, timeout=TIMEOUT_SECONDS
        ) as response:
            body = response.read()
    except TRANSPORT_ERRORS as exc:
        raise BridgeError(f"OwnTone API request failed: {exc}") from exc

    if not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BridgeError("OwnTone returned malformed JSON") from exc


def outputs() -> list[dict[str, object]]:
    response = request("/outputs")
    if not isinstance(response, dict) or not isinstance(response.get("outputs"), list):
        raise BridgeError("OwnTone returned an unexpected outputs response")
    items = response["outputs"]
    if not all(isinstance(item, dict) for item in items):
        raise BridgeError("OwnTone returned a malformed output entry")
    return items


def wiim_request(ip: str, command: str) -> dict[str, object]:
    encoded = urllib.parse.quote(command, safe="")
    req = urllib.request.Request(
        f"https://{ip}/httpapi.asp?command={encoded}", method="GET"
    )
    # WiiM devices use self-signed LAN certificates. The destination is a
    # validated, configured IPv4 address inside the private TRUSTED_NETWORK.
    context = ssl._create_unverified_context()  # nosec B323
    try:
        with urllib.request.urlopen(  # nosec B310
            req, timeout=TIMEOUT_SECONDS, context=context
        ) as response:
            payload = json.load(response)
    except TRANSPORT_ERRORS as exc:
        raise BridgeError(f"WiiM request failed for {ip}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BridgeError(f"WiiM {ip} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise BridgeError(f"WiiM {ip} returned an unexpected response")
    return payload


def reported_followers(leader_ip: str) -> set[tuple[str, str]]:
    """Return the (address, name) pairs the leader says it is driving."""
    response = wiim_request(leader_ip, "multiroom:getSlaveList")
    entries = response.get("slave_list")
    if not isinstance(entries, list):
        raise BridgeError(f"WiiM {leader_ip} returned no follower list")
    followers: set[tuple[str, str]] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        ip, name = item.get("ip"), item.get("name")
        if isinstance(ip, str) and isinstance(name, str):
            followers.add((ip, name))
    return followers


def describe(members: set[tuple[str, str]]) -> str:
    return ", ".join(f"{name} ({ip})" for ip, name in sorted(members)) or "none"


def validate_wiim_group() -> None:
    """Fail closed unless the native group exactly matches the configuration."""
    config = get_config()
    leader = wiim_request(config.kitchen_ip, "getStatusEx")
    if str(leader.get("group")) != "0":
        raise BridgeError(
            f"{config.kitchen_device_name} ({config.kitchen_ip}) is not acting as "
            "the group leader; refusing to select the WiiM output"
        )

    expected = set(config.followers)
    reported = reported_followers(config.kitchen_ip)
    if reported != expected:
        missing = describe(expected - reported)
        unexpected = describe(reported - expected)
        raise BridgeError(
            f"{config.kitchen_device_name} is not leading the configured group; "
            f"missing: {missing}; unexpected: {unexpected}. Refusing to select "
            "the WiiM output"
        )

    for ip, name in config.followers:
        status = wiim_request(ip, "getStatusEx")
        if str(status.get("group")) != "1":
            raise BridgeError(
                f"{name} ({ip}) is not joined to a group; refusing to select the "
                "WiiM output"
            )
        if status.get("master_ip") != config.kitchen_ip:
            raise BridgeError(
                f"{name} ({ip}) follows {status.get('master_ip')!r} rather than "
                f"{config.kitchen_device_name} ({config.kitchen_ip}); refusing to "
                "select the WiiM output"
            )


def find_output(
    items: list[dict[str, object]], name: str, output_type: str
) -> dict[str, object]:
    matches = [
        item
        for item in items
        if item.get("name") == name and item.get("type") == output_type
    ]
    if len(matches) != 1:
        raise BridgeError(
            f"Expected exactly one {output_type} output named {name!r}; "
            f"found {len(matches)}"
        )
    output = matches[0]
    # Reject a malformed entry now rather than part-way through a change.
    output_id(output)
    return output


def resolve_target(items: list[dict[str, object]], target: str) -> dict[str, object]:
    return find_output(items, *target_output(target))


def resolve_pair(
    items: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    local = resolve_target(items, "local")
    wiim = resolve_target(items, "wiim")
    if output_id(local) == output_id(wiim):
        raise BridgeError("configured PC and WiiM outputs share the same id")
    return local, wiim


def output_id(output: dict[str, object]) -> str:
    value = output.get("id")
    if isinstance(value, bool) or not isinstance(value, int | str) or not str(value):
        raise BridgeError("OwnTone output is missing its id")
    return str(value)


def output_integer(output: dict[str, object], field: str) -> int:
    try:
        value = output[field]
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
        raise ValueError
    except (KeyError, ValueError) as exc:
        raise BridgeError(f"OwnTone output has an invalid {field}") from exc


def set_outputs(selected: list[dict[str, object]]) -> None:
    request(
        "/outputs/set",
        method="PUT",
        payload={"outputs": [output_id(item) for item in selected]},
    )


def set_output_volume(output: dict[str, object], volume: int) -> None:
    low, high = VOLUME_RANGE
    if not low <= volume <= high:
        raise BridgeError(f"volume must be between {low} and {high}")
    query = urllib.parse.urlencode({"volume": volume, "output_id": output_id(output)})
    request(f"/player/volume?{query}", method="PUT")


def set_output_offset(output: dict[str, object], offset_ms: int) -> None:
    low, high = OFFSET_RANGE_MS
    if not low <= offset_ms <= high:
        raise BridgeError(f"offset must be between {low} and {high} milliseconds")
    identifier = urllib.parse.quote(output_id(output), safe="")
    request(f"/outputs/{identifier}", method="PUT", payload={"offset_ms": offset_ms})


def show_status() -> None:
    items = outputs()
    print(f"{'SEL':3}  {'VOL':>3}  {'OFFSET':>7}  {'TYPE':10}  NAME")
    for item in items:
        mark = "yes" if item.get("selected") else "no"
        volume = output_integer(item, "volume")
        offset_ms = output_integer(item, "offset_ms")
        print(
            f"{mark:3}  {volume:>3}  "
            f"{offset_ms:>6}ms  "
            f"{item.get('type', '')!s:10}  {item.get('name', '')}"
        )


def select_local() -> None:
    with output_control.locked():
        items = outputs()
        local = resolve_target(items, "local")
        output_control.set_mode("local")
        set_outputs([local])
    print(f"Selected only {get_config().local_output_name}.")


def select_all(confirmed: bool) -> None:
    if not confirmed:
        raise BridgeError(
            "Selecting the WiiM leader replaces its active source. Re-run with "
            "--confirm-wiim-takeover after a brief interruption is acceptable."
        )
    with output_control.locked():
        validate_wiim_group()
        config = get_config()
        items = outputs()
        local, wiim = resolve_pair(items)
        set_outputs([local, wiim])
        output_control.set_automatic(True)
    print(f"Selected {config.local_output_name} and {config.wiim_output_name}.")


def stop() -> None:
    with output_control.locked():
        output_control.set_mode("stopped")
        set_outputs([])
    print("Deselected every OwnTone output; WiiM group membership was not changed.")


def set_offset(target: str, offset_ms: int) -> None:
    items = outputs()
    output = resolve_target(items, target)
    set_output_offset(output, offset_ms)
    print(f"Set {output.get('name')} offset to {offset_ms} ms.")


def set_volume(target: str, volume: int) -> None:
    items = outputs()
    output = resolve_target(items, target)
    set_output_volume(output, volume)
    print(f"Set {output.get('name')} volume to {volume}%.")


def reconcile_once(*, activate: bool = True) -> None:
    """Restore the tested topology, selections, levels, and sync offsets."""
    validate_wiim_group()
    config = get_config()
    items = outputs()
    local, wiim = resolve_pair(items)

    # Deselect first so the outputs are certainly idle, then apply levels before
    # reconnecting. Without the deselect, OwnTone may already have restored its
    # cached selection at a cached volume, and the burst this is meant to
    # prevent has already happened.
    set_outputs([])
    set_output_volume(local, config.local_volume)
    set_output_volume(wiim, config.wiim_volume)
    set_output_offset(local, config.local_offset_ms)
    set_output_offset(wiim, config.wiim_offset_ms)
    if activate:
        set_outputs([local, wiim])

    refreshed = outputs()
    refreshed_local, refreshed_wiim = resolve_pair(refreshed)
    expected_ids = {output_id(local), output_id(wiim)} if activate else set()
    selected_ids = {
        output_id(item) for item in refreshed if item.get("selected") is True
    }
    checks = (
        selected_ids == expected_ids,
        output_integer(refreshed_local, "volume") == config.local_volume,
        output_integer(refreshed_wiim, "volume") == config.wiim_volume,
        output_integer(refreshed_local, "offset_ms") == config.local_offset_ms,
        output_integer(refreshed_wiim, "offset_ms") == config.wiim_offset_ms,
    )
    if not all(checks):
        raise BridgeError("OwnTone did not retain the reconciled output state")


def reconcile(
    wait_seconds: int, *, activate: bool = True, respect_manual: bool = False
) -> None:
    if wait_seconds < 0 or wait_seconds > 600:
        raise BridgeError("wait-seconds must be between 0 and 600")
    config = get_config()
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with output_control.locked():
                if respect_manual and not output_control.automatic_enabled():
                    print(
                        "Manual output control is active; leaving output selection unchanged."
                    )
                    return
                reconcile_once(activate=activate)
                output_control.set_automatic(True)
            print(
                f"Reconciled {config.kitchen_device_name} leader -> "
                f"{describe(set(config.followers))}; "
                + (
                    f"selected PC at {config.local_volume}% and WiiM at "
                    f"{config.wiim_volume}%."
                    if activate
                    else "left AirPlay outputs disconnected while Spotify is idle."
                )
            )
            return
        except (BridgeError, OSError, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise BridgeError(
                    f"reconciliation did not become ready within {wait_seconds}s: {exc}"
                ) from exc
            time.sleep(2)


def resume_pcm_playback() -> bool:
    """Resume our FIFO after an output failure paused OwnTone's player."""
    player = request("/player")
    if not isinstance(player, dict) or player.get("state") not in (
        "play",
        "pause",
        "stop",
    ):
        raise BridgeError("OwnTone returned an unknown player state")
    if player["state"] == "play":
        return False
    queue = request("/queue")
    if not isinstance(queue, dict) or not isinstance(queue.get("items"), list):
        raise BridgeError("OwnTone returned an unknown playback queue")
    items = queue["items"]
    current_id = player.get("item_id")
    current = [
        item
        for item in items
        if isinstance(item, dict)
        and (
            str(item.get("id")) == str(current_id)
            or (current_id is None and len(items) == 1)
        )
    ]
    if (
        len(current) != 1
        or current[0].get("data_kind") != "pipe"
        or current[0].get("path") != "/srv/media/spotify.pcm"
    ):
        raise BridgeError(
            "refusing to resume an OwnTone queue other than the bridge PCM pipe"
        )
    request("/player/play", method="PUT")
    refreshed = request("/player")
    if not isinstance(refreshed, dict) or refreshed.get("state") != "play":
        raise BridgeError("OwnTone PCM player did not resume")
    return True


def pause_pcm_playback() -> bool:
    """Stop a stale local session when Soloist is no longer the active source."""
    player = request("/player")
    if not isinstance(player, dict) or player.get("state") not in (
        "play",
        "pause",
        "stop",
    ):
        raise BridgeError("OwnTone returned an unknown player state")
    if player["state"] != "play":
        return False
    queue = request("/queue")
    if not isinstance(queue, dict) or not isinstance(queue.get("items"), list):
        raise BridgeError("OwnTone returned an unknown playback queue")
    current = [
        item
        for item in queue["items"]
        if isinstance(item, dict) and str(item.get("id")) == str(player.get("item_id"))
    ]
    if (
        len(current) != 1
        or current[0].get("data_kind") != "pipe"
        or current[0].get("path") != "/srv/media/spotify.pcm"
    ):
        raise BridgeError(
            "refusing to pause an OwnTone queue other than the bridge PCM pipe"
        )
    request("/player/pause", method="PUT")
    refreshed = request("/player")
    if not isinstance(refreshed, dict) or refreshed.get("state") != "pause":
        raise BridgeError("OwnTone PCM player did not pause")
    return True


def show_group_status() -> None:
    validate_wiim_group()
    config = get_config()
    print(
        f"WiiM group is healthy: {config.kitchen_device_name} leader -> "
        f"{describe(set(config.followers))}."
    )


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show all OwnTone outputs")
    sub.add_parser("group-status", help="verify the native WiiM group topology")
    sub.add_parser("select-local", help="select only this PC's DisplayPort output")
    all_parser = sub.add_parser("select-all", help="select the PC and WiiM group")
    all_parser.add_argument(
        "--confirm-wiim-takeover",
        action="store_true",
        help="acknowledge that AirPlay will replace the leader's active source",
    )
    sub.add_parser("stop", help="deselect all outputs without changing the WiiM group")
    volume_parser = sub.add_parser("set-volume", help="set an output's volume")
    volume_parser.add_argument("target", choices=("local", "wiim"))
    volume_parser.add_argument("percent", type=int)
    offset_parser = sub.add_parser("set-offset", help="set an output's sync delay")
    offset_parser.add_argument("target", choices=("local", "wiim"))
    offset_parser.add_argument("milliseconds", type=int)
    reconcile_parser = sub.add_parser(
        "reconcile", help="restore the configured group, outputs, volumes, and offsets"
    )
    reconcile_parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="retry while OwnTone, the LAN, and WiiMs become ready (maximum 600)",
    )
    return cli


COMMANDS = {
    "status": lambda args: show_status(),
    "group-status": lambda args: show_group_status(),
    "select-local": lambda args: select_local(),
    "select-all": lambda args: select_all(args.confirm_wiim_takeover),
    "stop": lambda args: stop(),
    "set-volume": lambda args: set_volume(args.target, args.percent),
    "set-offset": lambda args: set_offset(args.target, args.milliseconds),
    "reconcile": lambda args: reconcile(args.wait_seconds),
}


def main() -> int:
    args = parser().parse_args()
    handler = COMMANDS.get(args.command)
    if handler is None:
        print(f"error: unhandled command {args.command!r}", file=sys.stderr)
        return 2
    try:
        handler(args)
    except ConfigError as exc:
        print(f"error: invalid configuration: {exc}", file=sys.stderr)
        return 2
    except (BridgeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
