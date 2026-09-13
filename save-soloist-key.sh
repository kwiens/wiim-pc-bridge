#!/bin/sh
set -eu

project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
config_file=${WIIM_BRIDGE_ENV:-$project_directory/.env}
if [ -f "$config_file" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$config_file"
  set +a
fi

key_file=${SOLOIST_KEY_FILE:-$HOME/.config/wiim-pc-bridge/soloist_api_key}
key_directory=$(dirname -- "$key_file")
project_key_directory="$project_directory/.secrets"
project_key_file="$project_key_directory/soloist_api_key"
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
IFS= read -r api_key
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
mkdir -p "$key_directory"
chmod 0700 "$key_directory"
temporary_file=$(mktemp "$key_directory/.soloist_api_key.XXXXXX")
printf '%s\n' "$api_key" > "$temporary_file"
chmod 0600 "$temporary_file"
mv -f "$temporary_file" "$key_file"
temporary_file=""
install -d -m 0700 "$project_key_directory"
install -m 0600 "$key_file" "$project_key_file"
echo "Saved the private home key and ignored project backup with mode 600."
