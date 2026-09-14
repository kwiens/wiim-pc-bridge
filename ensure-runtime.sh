#!/bin/sh
set -eu

project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
config_file=${WIIM_BRIDGE_ENV:-$project_directory/.env}
if [ ! -f "$config_file" ]; then
  echo "Missing $config_file; copy .env.example to .env and edit it." >&2
  exit 1
fi

# Read the configuration through the single strict parser rather than sourcing
# it. Sourcing executes the file, so a value Docker Compose treats literally --
# parentheses, $VAR, backticks -- would be expanded or would abort the script,
# and the shell would silently disagree with the Python tools about the same
# line. --export emits shell-quoted assignments from the validated config.
if ! exported=$("$project_directory/render-config.py" --export); then
  echo "Could not read $config_file; see the error above." >&2
  exit 1
fi
# shellcheck disable=SC2154  # BRIDGE_UID and friends are set by the eval above.
eval "$exported"

audio_fifo="$project_directory/media/spotify.pcm"

if [ "$(id -u)" -ne "$BRIDGE_UID" ]; then
  echo "Run this setup as the configured uid $BRIDGE_UID, not as $(id -u)." >&2
  exit 1
fi

umask 077
mkdir -p \
  "$project_directory/media" \
  "$project_directory/cache/owntone" \
  "$project_directory/cache/soloist/data" \
  "$project_directory/cache/soloist/cache" \
  "$project_directory/runtime"
chmod 0700 \
  "$project_directory/cache/soloist/data" \
  "$project_directory/cache/soloist/cache" \
  "$project_directory/runtime"
chmod 0600 "$config_file"

if [ -e "$audio_fifo" ] && [ ! -p "$audio_fifo" ]; then
  echo "$audio_fifo exists but is not a named pipe." >&2
  exit 1
fi
if [ ! -p "$audio_fifo" ]; then
  mkfifo "$audio_fifo"
fi
chmod 0660 "$audio_fifo"

if [ ! -s "$SOLOIST_KEY_FILE" ]; then
  echo "Soloist API key is missing; run ./save-soloist-key.sh first." >&2
  exit 1
fi
chmod 0600 "$SOLOIST_KEY_FILE"

if [ ! -f "$PULSE_COOKIE_PATH" ]; then
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

export XDG_RUNTIME_DIR="$BRIDGE_RUNTIME_DIR"
export PULSE_SERVER="unix:$BRIDGE_RUNTIME_DIR/pulse/native"
attempt=0
until [ -S "$BRIDGE_RUNTIME_DIR/pipewire-0" ] \
  && [ -S "$BRIDGE_RUNTIME_DIR/pulse/native" ] \
  && pactl info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "PipeWire was not ready after 60 seconds." >&2
    exit 1
  fi
  sleep 1
done

if ! pactl list short sinks | awk '{print $2}' | grep -Fxq "$DISPLAYPORT_SINK"; then
  echo "Configured DisplayPort sink is unavailable: $DISPLAYPORT_SINK" >&2
  exit 1
fi

echo "Runtime prerequisites are ready."
