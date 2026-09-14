#!/bin/sh
set -eu

project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
config_file=${WIIM_BRIDGE_ENV:-$project_directory/.env}

# Resolve the key path through the same strict parser the other tools use,
# rather than sourcing .env, which would execute it.
key_file=""
if [ -f "$config_file" ]; then
  if ! exported=$("$project_directory/render-config.py" --export); then
    echo "Could not read $config_file; see the error above." >&2
    exit 1
  fi
  # shellcheck disable=SC2154  # SOLOIST_KEY_FILE is set by the eval above.
  eval "$exported"
  key_file=$SOLOIST_KEY_FILE
fi
if [ -z "$key_file" ]; then
  key_file=$HOME/.config/wiim-pc-bridge/soloist_api_key
fi

key_directory=$(dirname -- "$key_file")
temporary_file=""
terminal_state=""

cleanup() {
  if [ -n "$terminal_state" ]; then
    stty "$terminal_state" 2>/dev/null || true
  fi
  if [ -n "$temporary_file" ] && [ -e "$temporary_file" ]; then
    rm -f "$temporary_file"
  fi
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ ! -t 0 ]; then
  echo "Run this helper in an interactive terminal." >&2
  exit 1
fi

printf 'Paste the Spotify Soloist API key (input is hidden): ' >&2
terminal_state=$(stty -g)
stty -echo
# `read` returns non-zero at EOF (Ctrl-D). Without this guard `set -e` would
# abort here and the user would see no explanation at all.
api_key=""
if ! IFS= read -r api_key; then
  api_key=""
fi
stty "$terminal_state"
terminal_state=""
printf '\n' >&2

if [ -z "$api_key" ]; then
  echo "No key entered; nothing was changed." >&2
  exit 1
fi

case "$api_key" in
  *[![:graph:]]*)
    echo "The key contains whitespace; nothing was changed." >&2
    exit 1
    ;;
esac

umask 077
# Only create and lock down a directory this script owns. `key_directory` is
# whatever the user configured, so chmod-ing it unconditionally could relock an
# unrelated directory -- $HOME, for instance, if the key sits directly in it.
if [ ! -d "$key_directory" ]; then
  mkdir -p "$key_directory"
  chmod 0700 "$key_directory"
fi
temporary_file=$(mktemp "$key_directory/.soloist_api_key.XXXXXX")
printf '%s\n' "$api_key" > "$temporary_file"
chmod 0600 "$temporary_file"
mv -f "$temporary_file" "$key_file"
temporary_file=""
echo "Saved the Soloist API key to $key_file with mode 600."
