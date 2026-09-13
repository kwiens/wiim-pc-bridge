#!/bin/sh
set -eu

project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
template="$project_directory/systemd/wiim-pc-bridge.service.in"
unit_directory=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
unit_file="$unit_directory/wiim-pc-bridge.service"
temporary_file=""
backup_file=""
unit_replaced=0
verification_output=""
previous_enabled=0
previous_active=0

cleanup() {
  if [ "$unit_replaced" -eq 1 ]; then
    if [ -n "$backup_file" ] && [ -e "$backup_file" ]; then
      cp "$backup_file" "$unit_file"
    else
      rm -f "$unit_file"
    fi
    systemctl --user daemon-reload || true
    if [ "$previous_enabled" -eq 1 ]; then
      systemctl --user enable wiim-pc-bridge.service || true
    else
      systemctl --user disable wiim-pc-bridge.service || true
    fi
    if [ "$previous_active" -eq 1 ]; then
      systemctl --user restart wiim-pc-bridge.service || true
    else
      systemctl --user stop wiim-pc-bridge.service || true
    fi
  fi
  if [ -n "$temporary_file" ] && [ -e "$temporary_file" ]; then
    rm -f "$temporary_file"
  fi
  if [ -n "$backup_file" ] && [ -e "$backup_file" ]; then
    rm -f "$backup_file"
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

case "$project_directory" in
  *[[:space:]]* | *'|'* | *'&'* | *'"'* | *\\*)
    echo "The project path contains whitespace or an unsupported character." >&2
    exit 1
    ;;
esac

mkdir -p "$unit_directory"
if systemctl --user is-enabled --quiet wiim-pc-bridge.service 2>/dev/null; then
  previous_enabled=1
fi
if systemctl --user is-active --quiet wiim-pc-bridge.service 2>/dev/null; then
  previous_active=1
fi
temporary_file=$(mktemp "$unit_directory/wiim-pc-bridge.XXXXXX.service")
sed "s|@PROJECT_DIRECTORY@|$project_directory|g" "$template" > "$temporary_file"
chmod 0644 "$temporary_file"
if ! verification_output=$(systemd-analyze --user verify "$temporary_file" 2>&1); then
  printf '%s\n' "$verification_output" >&2
  exit 1
fi

if [ -e "$unit_file" ]; then
  backup_file=$(mktemp "$unit_directory/.wiim-pc-bridge.backup.XXXXXX")
  cp "$unit_file" "$backup_file"
fi
mv -f "$temporary_file" "$unit_file"
temporary_file=""
unit_replaced=1

systemctl --user daemon-reload
systemctl --user enable wiim-pc-bridge.service
systemctl --user restart wiim-pc-bridge.service
unit_replaced=0
echo "Installed and started $unit_file"
