#!/bin/sh
set -eu

audio_fifo=/srv/media/spotify.pcm
sink_name=${BRIDGE_SINK:-wiim_bridge}
if [ ! -p "$audio_fifo" ]; then
  echo "Bridge PCM path is not a FIFO: $audio_fifo" >&2
  exit 1
fi

attempt=0
until pactl list short sinks | awk '{print $2}' | grep -Fxq "$sink_name"; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Bridge sink did not become available" >&2
    exit 1
  fi
  sleep 1
done

# Keep reading Pulse even when OwnTone pauses or closes its FIFO reader.
# The relay drops undeliverable blocks instead of letting parec accumulate
# unbounded buffered audio and abort at PulseAudio's allocation limit.
exec /usr/bin/python3 /usr/local/bin/capture_relay.py "$sink_name" "$audio_fifo"
