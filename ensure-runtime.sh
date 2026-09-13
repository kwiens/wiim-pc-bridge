#!/bin/sh
set -eu

project_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
config_file=${WIIM_BRIDGE_ENV:-$project_directory/.env}
if [ ! -f "$config_file" ]; then
  echo "Missing $config_file; copy .env.example to .env and edit it." >&2
  exit 1
fi
# The local configuration is shell-compatible and trusted by this user.
# shellcheck source=/dev/null
set -a
. "$config_file"
set +a

bridge_uid=${BRIDGE_UID:-$(id -u)}
runtime_directory=${BRIDGE_RUNTIME_DIR:-/run/user/$bridge_uid}
home_key=${SOLOIST_KEY_FILE:-$HOME/.config/wiim-pc-bridge/soloist_api_key}
project_key_directory="$project_directory/.secrets"
project_key="$project_key_directory/soloist_api_key"
audio_fifo="$project_directory/media/spotify.pcm"

if [ "$(id -u)" -ne "$bridge_uid" ]; then
  echo "Run this setup as the configured uid $bridge_uid, not as $(id -u)." >&2
  exit 1
fi

umask 077
mkdir -p \
  "$project_directory/media" \
  "$project_directory/cache/owntone" \
  "$project_directory/cache/soloist/data" \
  "$project_directory/cache/soloist/cache" \
  "$project_key_directory" \
  "$project_directory/runtime"
chmod 0700 \
  "$project_directory/cache/soloist/data" \
  "$project_directory/cache/soloist/cache" \
  "$project_key_directory"

if [ -e "$audio_fifo" ] && [ ! -p "$audio_fifo" ]; then
  echo "$audio_fifo exists but is not a named pipe." >&2
  exit 1
fi
if [ ! -p "$audio_fifo" ]; then
  mkfifo "$audio_fifo"
fi
chmod 0660 "$audio_fifo"

if [ ! -s "$home_key" ]; then
  echo "Soloist API key is missing; run ./save-soloist-key.sh first." >&2
  exit 1
fi
chmod 0600 "$home_key"
install -m 0600 "$home_key" "$project_key"

if [ ! -f "${PULSE_COOKIE_PATH:?Set PULSE_COOKIE_PATH in .env}" ]; then
  echo "Pulse cookie is missing: $PULSE_COOKIE_PATH" >&2
  exit 1
fi

"$project_directory/render-config.py"

attempt=0
until docker info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Docker was not ready after 120 seconds." >&2
    exit 1
  fi
  sleep 2
done

export XDG_RUNTIME_DIR="$runtime_directory"
export PULSE_SERVER="unix:$runtime_directory/pulse/native"
attempt=0
until [ -S "$runtime_directory/pipewire-0" ] \
  && [ -S "$runtime_directory/pulse/native" ] \
  && pactl info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "PipeWire was not ready after 60 seconds." >&2
    exit 1
  fi
  sleep 1
done

if ! pactl list short sinks | awk '{print $2}' \
  | grep -Fxq "${DISPLAYPORT_SINK:?Set DISPLAYPORT_SINK in .env}"; then
  echo "Configured DisplayPort sink is unavailable: $DISPLAYPORT_SINK" >&2
  exit 1
fi

echo "Runtime prerequisites are ready."
