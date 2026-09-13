#!/bin/sh
set -eu

project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
template="$project_directory/systemd/wiim-pc-bridge.service.in"
unit_directory=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
unit_file="$unit_directory/wiim-pc-bridge.service"
temporary_file=""

cleanup() {
  if [ -n "$temporary_file" ] && [ -e "$temporary_file" ]; then
    rm -f "$temporary_file"
  fi
}
trap cleanup EXIT HUP INT TERM

case "$project_directory" in
  *'|'* | *'&'* | *\\*)
    echo "The project path contains a character unsupported by this installer." >&2
    exit 1
    ;;
esac

mkdir -p "$unit_directory"
temporary_file=$(mktemp "$unit_directory/.wiim-pc-bridge.service.XXXXXX")
sed "s|@PROJECT_DIRECTORY@|$project_directory|g" "$template" > "$temporary_file"
chmod 0644 "$temporary_file"
mv -f "$temporary_file" "$unit_file"
temporary_file=""

systemctl --user daemon-reload
systemctl --user enable wiim-pc-bridge.service
systemctl --user restart wiim-pc-bridge.service
echo "Installed and started $unit_file"
