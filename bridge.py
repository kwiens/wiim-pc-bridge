#!/usr/bin/env python3
"""Safe OwnTone output control for the PC + WiiM audio bridge."""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from bridge_config import CONFIG

API = "http://127.0.0.1:3689/api"
LOCAL_NAME = CONFIG.local_output_name
WIIM_NAME = CONFIG.wiim_output_name
KITCHEN_IP = CONFIG.kitchen_ip
LIVING_ROOM_IP = CONFIG.living_room_ip


class BridgeError(RuntimeError):
    pass


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
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310
            body = response.read()
    except urllib.error.URLError as exc:
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
    return response["outputs"]


def wiim_request(ip: str, command: str) -> dict[str, object]:
    encoded = urllib.parse.quote(command, safe="")
    req = urllib.request.Request(
        f"https://{ip}/httpapi.asp?command={encoded}", method="GET"
    )
    # WiiM devices use self-signed LAN certificates. The destination is a
    # validated, configured IPv4 address inside TRUSTED_NETWORK.
    context = ssl._create_unverified_context()  # nosec B323
    try:
        with urllib.request.urlopen(  # nosec B310
            req, timeout=5, context=context
        ) as response:
            payload = json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise BridgeError(f"WiiM request failed for {ip}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BridgeError(f"WiiM {ip} returned an unexpected response")
    return payload


def validate_wiim_group() -> None:
    kitchen = wiim_request(KITCHEN_IP, "getStatusEx")
    living = wiim_request(LIVING_ROOM_IP, "getStatusEx")
    followers = wiim_request(KITCHEN_IP, "multiroom:getSlaveList")
    follower_list = followers.get("slave_list")
    expected_follower = isinstance(follower_list, list) and any(
        isinstance(item, dict)
        and item.get("ip") == LIVING_ROOM_IP
        and item.get("name") == CONFIG.living_room_device_name
        for item in follower_list
    )
    if not (
        str(kitchen.get("group")) == "0"
        and str(living.get("group")) == "1"
        and living.get("master_ip") == KITCHEN_IP
        and followers.get("slaves") == 1
        and expected_follower
    ):
        raise BridgeError(
            f"WiiM group is not {CONFIG.kitchen_device_name} leader -> "
            f"{CONFIG.living_room_device_name} follower; refusing to select "
            "the WiiM output"
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
    output_id(output)
    return output


def output_id(output: dict[str, object]) -> str:
    value = output.get("id")
    if value is None or not str(value):
        raise BridgeError("OwnTone output is missing its id")
    return str(value)


def output_integer(output: dict[str, object], field: str) -> int:
    try:
        return int(output[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeError(f"OwnTone output has an invalid {field}") from exc


def set_outputs(selected: list[dict[str, object]]) -> None:
    request(
        "/outputs/set",
        method="PUT",
        payload={"outputs": [output_id(item) for item in selected]},
    )


def set_output_volume(output: dict[str, object], volume: int) -> None:
    if not 0 <= volume <= 100:
        raise BridgeError("volume must be between 0 and 100")
    query = urllib.parse.urlencode({"volume": volume, "output_id": output_id(output)})
    request(f"/player/volume?{query}", method="PUT")


def set_output_offset(output: dict[str, object], offset_ms: int) -> None:
    if not -2000 <= offset_ms <= 2000:
        raise BridgeError("offset must be between -2000 and 2000 milliseconds")
    request(
        f"/outputs/{output_id(output)}",
        method="PUT",
        payload={"offset_ms": offset_ms},
    )


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
    items = outputs()
    local = find_output(items, LOCAL_NAME, "AirPlay 1")
    set_outputs([local])
    print(f"Selected only {LOCAL_NAME}.")


def select_all(confirmed: bool) -> None:
    if not confirmed:
        raise BridgeError(
            "Selecting the WiiM leader replaces its active source. Re-run with "
            "--confirm-wiim-takeover after a brief interruption is acceptable."
        )
    validate_wiim_group()
    items = outputs()
    local = find_output(items, LOCAL_NAME, "AirPlay 1")
    wiim = find_output(items, WIIM_NAME, "AirPlay 2")
    set_outputs([local, wiim])
    print(f"Selected {LOCAL_NAME} and {WIIM_NAME}.")


def stop() -> None:
    set_outputs([])
    print("Deselected every OwnTone output; WiiM group membership was not changed.")


def set_offset(target: str, offset_ms: int) -> None:
    items = outputs()
    name = LOCAL_NAME if target == "local" else WIIM_NAME
    output_type = "AirPlay 1" if target == "local" else "AirPlay 2"
    output = find_output(items, name, output_type)
    set_output_offset(output, offset_ms)
    print(f"Set {name} offset to {offset_ms} ms.")


def set_volume(target: str, volume: int) -> None:
    items = outputs()
    name = LOCAL_NAME if target == "local" else WIIM_NAME
    output_type = "AirPlay 1" if target == "local" else "AirPlay 2"
    output = find_output(items, name, output_type)
    set_output_volume(output, volume)
    print(f"Set {name} volume to {volume}%.")


def reconcile_once() -> None:
    """Restore the tested topology, selections, levels, and sync offsets."""
    validate_wiim_group()
    items = outputs()
    local = find_output(items, LOCAL_NAME, "AirPlay 1")
    wiim = find_output(items, WIIM_NAME, "AirPlay 2")

    # Apply levels before connecting so stale cached values cannot produce an
    # unexpectedly loud burst when the outputs become active.
    set_output_volume(local, CONFIG.local_volume)
    set_output_volume(wiim, CONFIG.wiim_volume)
    set_output_offset(local, CONFIG.local_offset_ms)
    set_output_offset(wiim, CONFIG.wiim_offset_ms)
    set_outputs([local, wiim])

    refreshed = outputs()
    refreshed_local = find_output(refreshed, LOCAL_NAME, "AirPlay 1")
    refreshed_wiim = find_output(refreshed, WIIM_NAME, "AirPlay 2")
    expected_ids = {output_id(local), output_id(wiim)}
    selected_ids = {output_id(item) for item in refreshed if bool(item.get("selected"))}
    checks = (
        selected_ids == expected_ids,
        output_integer(refreshed_local, "volume") == CONFIG.local_volume,
        output_integer(refreshed_wiim, "volume") == CONFIG.wiim_volume,
        output_integer(refreshed_local, "offset_ms") == CONFIG.local_offset_ms,
        output_integer(refreshed_wiim, "offset_ms") == CONFIG.wiim_offset_ms,
    )
    if not all(checks):
        raise BridgeError("OwnTone did not retain the reconciled output state")


def reconcile(wait_seconds: int) -> None:
    if wait_seconds < 0 or wait_seconds > 600:
        raise BridgeError("wait-seconds must be between 0 and 600")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            reconcile_once()
            print(
                f"Reconciled {CONFIG.kitchen_device_name} leader -> "
                f"{CONFIG.living_room_device_name} follower; selected "
                f"PC at {CONFIG.local_volume}% and WiiM at {CONFIG.wiim_volume}%."
            )
            return
        except (BridgeError, OSError, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise BridgeError(
                    f"reconciliation did not become ready within {wait_seconds}s: {exc}"
                ) from exc
            time.sleep(2)


def show_group_status() -> None:
    validate_wiim_group()
    print(
        f"WiiM group is healthy: {CONFIG.kitchen_device_name} leader -> "
        f"{CONFIG.living_room_device_name} follower."
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


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "status":
            show_status()
        elif args.command == "group-status":
            show_group_status()
        elif args.command == "select-local":
            select_local()
        elif args.command == "select-all":
            select_all(args.confirm_wiim_takeover)
        elif args.command == "stop":
            stop()
        elif args.command == "set-volume":
            set_volume(args.target, args.percent)
        elif args.command == "set-offset":
            set_offset(args.target, args.milliseconds)
        elif args.command == "reconcile":
            reconcile(args.wait_seconds)
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
