#!/bin/sh
# ShellCheck cannot see that POSIX traps invoke these handlers.
# shellcheck disable=SC2317,SC2329
set -eu

api_key_file=/run/secrets/soloist_api_key
audio_fifo=/srv/media/spotify.pcm
sink_name=${BRIDGE_SINK:-wiim_bridge}
device_name=${SOLOIST_DEVICE_NAME:-PC + WiiM}
volume_file=/var/lib/soloist/handoff-volume
initial_volume=40
module_id=""
capture_pid=""
soloist_pid=""
stop_requested=0
capture_ready_timeout=30

cleanup() {
  if [ -n "$capture_pid" ]; then
    kill "$capture_pid" 2>/dev/null || true
    wait "$capture_pid" 2>/dev/null || true
  fi
  if [ -n "$module_id" ]; then
    pactl unload-module "$module_id" 2>/dev/null || true
  fi
  rm -f /tmp/parec.pid /tmp/soloist-child.pid
}

forward_signal() {
  stop_requested=1
  if [ -n "$soloist_pid" ]; then
    kill -TERM "$soloist_pid" 2>/dev/null || true
    return
  fi
  # No child exists yet. Trapping replaced the default terminate action, so
  # without this the script would ignore the stop and go on to start Soloist.
  echo "Stop requested before startup finished; exiting." >&2
  exit 143
}

trap cleanup EXIT
trap forward_signal INT TERM HUP

if [ ! -s "$api_key_file" ]; then
  echo "Spotify Soloist API key is missing: $api_key_file" >&2
  exit 1
fi
if [ ! -e "$audio_fifo" ]; then
  mkfifo "$audio_fifo"
  chmod 0660 "$audio_fifo"
elif [ ! -p "$audio_fifo" ]; then
  echo "OwnTone PCM FIFO is missing: $audio_fifo" >&2
  exit 1
fi

api_key=$(tr -d '\r\n' < "$api_key_file")
if [ -z "$api_key" ]; then
  echo "Spotify Soloist API key is empty" >&2
  exit 1
fi

# Start at the last stable Spotify source volume. The host monitor refreshes
# this private state file while Soloist is inactive and corrects the live level
# as soon as a handoff occurs.
if [ -r "$volume_file" ]; then
  saved_volume=$(tr -d '[:space:]' < "$volume_file")
  case "$saved_volume" in
    '' | *[!0-9]*)
      echo "Ignoring invalid saved Spotify volume" >&2
      ;;
    *)
      if [ "${#saved_volume}" -le 3 ] && [ "$saved_volume" -le 100 ]; then
        initial_volume=$saved_volume
      else
        echo "Ignoring invalid saved Spotify volume" >&2
      fi
      ;;
  esac
fi

# A user service can start while PipeWire is still settling after boot. Wait
# for the Pulse compatibility server instead of entering a restart storm.
attempt=0
until pactl info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "PipeWire Pulse server was not ready after 60 seconds" >&2
    exit 1
  fi
  sleep 1
done

# Create an isolated 44.1 kHz sink without changing the host's default output.
if pactl list short sinks | awk '{print $2}' | grep -Fxq "$sink_name"; then
  # A sink of this name already exists: a leaked module from a container that
  # was killed, or an unrelated host sink. Adopting it blindly would let parec
  # capture whatever that sink renders, so verify it carries the format this
  # bridge depends on.
  existing_spec=$(pactl list short sinks \
    | awk -v name="$sink_name" '$2 == name {print $4 " " $5}')
  case "$existing_spec" in
    "s16le 2ch"*)
      echo "Reusing the existing $sink_name sink ($existing_spec)." >&2
      ;;
    *)
      echo "A sink named $sink_name already exists with an unexpected format" \
        "($existing_spec); refusing to capture from it." >&2
      exit 1
      ;;
  esac
else
  module_id=$(pactl load-module module-null-sink \
    sink_name="$sink_name" \
    format=s16le \
    rate=44100 \
    channels=2 \
    sink_properties=device.description=Spotify_Bridge)
fi

# Capture only Soloist's sink monitor. parec performs any final format
# conversion and writes 44,100 Hz, signed 16-bit little-endian stereo PCM.
parec \
  --device="${sink_name}.monitor" \
  --format=s16le \
  --rate=44100 \
  --channels=2 \
  --latency-msec=100 \
  --process-time-msec=20 \
  --raw > "$audio_fifo" &
capture_pid=$!
echo "$capture_pid" > /tmp/parec.pid

# Opening a FIFO for writing blocks until a reader appears, so until OwnTone
# opens the pipe this PID is still the forked shell, not parec. Without this
# gate `kill -0` would report a live capture and the health check would go
# green while no PCM is flowing at all.
attempt=0
until [ "$(cat "/proc/$capture_pid/comm" 2>/dev/null || true)" = parec ]; do
  if ! kill -0 "$capture_pid" 2>/dev/null; then
    echo "PCM capture exited before it started" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge "$capture_ready_timeout" ]; then
    echo "No reader opened $audio_fifo after ${capture_ready_timeout}s;" \
      "OwnTone is not consuming the bridge pipe." >&2
    exit 1
  fi
  sleep 1
done

/usr/local/bin/soloist \
  --device-name "$device_name" \
  --api-key "$api_key" \
  --pipewire-device "$sink_name" \
  --initial-volume "$initial_volume" \
  --data-dir /var/lib/soloist \
  --cache-dir /var/cache/soloist \
  --cache-size 1024 \
  --ws 127.0.0.1:9090 &
soloist_pid=$!
echo "$soloist_pid" > /tmp/soloist-child.pid

if [ "$stop_requested" -eq 1 ]; then
  kill -TERM "$soloist_pid" 2>/dev/null || true
fi

# Supervise both children. Docker only restarts a container when PID 1 exits;
# a healthcheck alone does not recover a dead capture process.
while kill -0 "$soloist_pid" 2>/dev/null && kill -0 "$capture_pid" 2>/dev/null; do
  sleep 2
done

if ! kill -0 "$capture_pid" 2>/dev/null; then
  echo "PCM capture stopped unexpectedly; restarting the bridge source" >&2
  kill -TERM "$soloist_pid" 2>/dev/null || true
  wait "$soloist_pid" 2>/dev/null || true
  exit 1
fi

set +e
wait "$soloist_pid"
status=$?
# A trapped signal interrupts `wait`, which returns 128+signal WITHOUT reaping
# the child. Exiting here would drop PID 1 while Soloist is still flushing its
# bind-mounted state, and the kernel would SIGKILL it a few milliseconds into
# the shutdown, making stop_grace_period useless. Keep waiting until it is
# really gone.
while [ "$status" -gt 128 ] && kill -0 "$soloist_pid" 2>/dev/null; do
  wait "$soloist_pid"
  status=$?
done
set -e
exit "$status"
