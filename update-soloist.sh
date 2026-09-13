#!/bin/sh
set -eu

project_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
archive_url=https://soloist-builds.spotifycdn.com/soloist_release_x86_64.tar.gz
action=${1:---check}

case "$action" in
  --check|--apply) ;;
  *)
    echo "Usage: $0 [--check|--apply]" >&2
    exit 2
    ;;
esac

temporary_directory=$(mktemp -d /tmp/soloist-update.XXXXXX)
cleanup() {
  rm -r "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

archive="$temporary_directory/soloist.tar.gz"
curl --fail --silent --show-error --location "$archive_url" --output "$archive"
archive_sha=$(sha256sum "$archive" | awk '{print $1}')
tar --extract --gzip --file "$archive" --directory "$temporary_directory" soloist
chmod 0755 "$temporary_directory/soloist"
version_output=$(timeout 5 "$temporary_directory/soloist" --version)
version_number=$(printf '%s\n' "$version_output" | sed -nE 's/^soloist ([0-9.]+).*/\1/p')
build_date=$(printf '%s\n' "$version_output" | sed -nE 's/.*\(([0-9]{8})\).*/\1/p')

if [ -z "$version_number" ] || [ -z "$build_date" ]; then
  echo "Could not parse the downloaded Soloist version: $version_output" >&2
  exit 1
fi

current_sha=$(sed -nE 's/^ARG SOLOIST_ARCHIVE_SHA256=([0-9a-f]{64})$/\1/p' "$project_directory/Dockerfile.soloist")
candidate_tag="$version_number-$build_date"
expiry_date=$(date -d "${build_date} +90 days" +%F)

printf 'Current archive SHA-256:  %s\n' "$current_sha"
printf 'Downloaded Soloist:       %s\n' "$version_output"
printf 'Downloaded SHA-256:       %s\n' "$archive_sha"
printf 'Approximate expiry date:  %s\n' "$expiry_date"

if [ "$archive_sha" = "$current_sha" ]; then
  echo "The pinned Soloist build is current."
  exit 0
fi

if [ "$action" = --check ]; then
  echo "A different official build is available. Re-run with --apply to install it."
  exit 0
fi

sed -i -E \
  "s/^ARG SOLOIST_ARCHIVE_SHA256=[0-9a-f]{64}$/ARG SOLOIST_ARCHIVE_SHA256=$archive_sha/" \
  "$project_directory/Dockerfile.soloist"
sed -i -E \
  "s#^    image: wiim-pc-bridge/soloist:.*#    image: wiim-pc-bridge/soloist:$candidate_tag#" \
  "$project_directory/compose.yaml"

cd "$project_directory"
docker compose build soloist
docker compose up -d --no-deps --force-recreate soloist
"$project_directory/bridge.py" reconcile --wait-seconds 60
"$project_directory/doctor.py"
echo "Updated and validated Soloist $candidate_tag."
