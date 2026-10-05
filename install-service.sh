#!/bin/sh
set -eu

service_name=wiim-pc-bridge.service
units="wiim-pc-bridge-supervisor.service wiim-pc-bridge-volume.service wiim-pc-bridge.service"
case "$*" in
  '') ;;
  --keepalive) service_name=wiim-pc-bridge-keepalive.service; units="$service_name" ;;
  *) echo "Usage: $0 [--keepalive]" >&2; exit 2 ;;
esac
project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
case "$project_directory" in
  *[[:space:]]* | *'|'* | *'&'* | *'"'* | *"'"* | *';'* | *'%'* | *'$'* | *'`'* | *\\*)
    echo "Project path contains characters systemd would reinterpret" >&2
    exit 1 ;;
esac
unit_directory=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$unit_directory"
staging=$(mktemp -d "$unit_directory/.wiim-install.XXXXXX")
changed=""
committed=0
touched=0
previous_active=0
previous_enabled=0
systemctl --user is-active --quiet "$service_name" && previous_active=1
systemctl --user is-enabled --quiet "$service_name" 2>/dev/null && previous_enabled=1

cleanup() {
  if [ "$committed" -eq 0 ] && [ -n "$changed" ]; then
    for unit in $changed; do
      if [ -f "$staging/$unit.backup" ]; then
        cp "$staging/$unit.backup" "$unit_directory/$unit" || true
      else
        rm -f "$unit_directory/$unit"
      fi
    done
    systemctl --user daemon-reload || true
    if [ "$previous_enabled" -eq 0 ]; then
      systemctl --user disable "$service_name" || true
    fi
    if [ "$touched" -eq 1 ]; then
      if [ "$previous_active" -eq 1 ]; then
        systemctl --user restart "$service_name" || true
      else
        systemctl --user stop "$service_name" || true
      fi
    fi
  fi
  for unit in $units; do
    rm -f "$staging/$unit" "$staging/$unit.backup"
  done
  rmdir "$staging" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for unit in $units; do
  sed "s|@PROJECT_DIRECTORY@|$project_directory|g" \
    "$project_directory/systemd/$unit.in" > "$staging/$unit"
  chmod 0644 "$staging/$unit"
  if [ -f "$unit_directory/$unit" ]; then
    cp "$unit_directory/$unit" "$staging/$unit.backup"
  fi
done
# Resolve sibling unit dependencies from the staging directory during verify.
SYSTEMD_UNIT_PATH="$staging:" systemd-analyze --user verify "$staging/$service_name"
for unit in $units; do
  changed="$changed $unit"
  cp "$staging/$unit" "$unit_directory/$unit"
done
systemctl --user daemon-reload
systemctl --user enable "$service_name"
touched=1
systemctl --user restart "$service_name"
if [ "$service_name" = wiim-pc-bridge.service ]; then
  systemctl --user start wiim-pc-bridge-supervisor.service wiim-pc-bridge-volume.service
fi
committed=1
echo "Installed $units"
