#!/bin/sh
set -eu

project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
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
rollback_needed=0
container_changed=0
cleanup() {
  if [ "$rollback_needed" -eq 1 ]; then
    echo "Soloist update failed; restoring the previous pins." >&2
    cp "$temporary_directory/Dockerfile.soloist.previous" \
      "$project_directory/Dockerfile.soloist"
    cp "$temporary_directory/compose.yaml.previous" \
      "$project_directory/compose.yaml"
    if [ "$container_changed" -eq 1 ]; then
      (
        cd "$project_directory"
        docker compose up -d --no-deps --force-recreate \
          --wait --wait-timeout 180 soloist
      ) || echo "Automatic container rollback failed; run docker compose up -d soloist." >&2
    fi
  fi
  rm -r "$temporary_directory"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

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
current_tag=$(sed -nE 's#^    image: wiim-pc-bridge/soloist:(.+)$#\1#p' \
  "$project_directory/compose.yaml")
candidate_tag="$version_number-$build_date"
expiry_date=$(date -d "${build_date} +90 days" +%F)

if [ -z "$current_sha" ] || [ -z "$current_tag" ]; then
  echo "Could not read the current Soloist pins; no files were changed." >&2
  exit 1
fi

printf 'Current archive SHA-256:  %s\n' "$current_sha"
printf 'Downloaded Soloist:       %s\n' "$version_output"
printf 'Downloaded SHA-256:       %s\n' "$archive_sha"
printf 'Approximate expiry date:  %s\n' "$expiry_date"

if [ "$archive_sha" = "$current_sha" ]; then
  echo "The pinned Soloist build is current."
  exit 0
fi

if [ "$candidate_tag" = "$current_tag" ]; then
  echo "The archive changed without a new version/build identifier; refusing it." >&2
  exit 1
fi

if [ "$action" = --check ]; then
  echo "A different official build is available. Re-run with --apply to install it."
  exit 0
fi

cp "$project_directory/Dockerfile.soloist" \
  "$temporary_directory/Dockerfile.soloist.previous"
cp "$project_directory/compose.yaml" \
  "$temporary_directory/compose.yaml.previous"
rollback_needed=1

sed -i -E \
  "s/^ARG SOLOIST_ARCHIVE_SHA256=[0-9a-f]{64}$/ARG SOLOIST_ARCHIVE_SHA256=$archive_sha/" \
  "$project_directory/Dockerfile.soloist"
sed -i -E \
  "s#^    image: wiim-pc-bridge/soloist:.*#    image: wiim-pc-bridge/soloist:$candidate_tag#" \
  "$project_directory/compose.yaml"

updated_sha=$(sed -nE 's/^ARG SOLOIST_ARCHIVE_SHA256=([0-9a-f]{64})$/\1/p' \
  "$project_directory/Dockerfile.soloist")
updated_tag=$(sed -nE 's#^    image: wiim-pc-bridge/soloist:(.+)$#\1#p' \
  "$project_directory/compose.yaml")
if [ "$updated_sha" != "$archive_sha" ] || [ "$updated_tag" != "$candidate_tag" ]; then
  echo "Could not update the Soloist manifest pins." >&2
  exit 1
fi

cd "$project_directory"
docker compose build soloist
container_changed=1
docker compose up -d --no-deps --force-recreate soloist

attempt=0
until [ "$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    wiim-pc-bridge-soloist 2>/dev/null || true)" = healthy ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 12 ]; then
    echo "The updated Soloist container did not become healthy." >&2
    exit 1
  fi
  sleep 5
done

rollback_needed=0
echo "Updated and health-checked Soloist $candidate_tag."
echo "Reconnect Spotify if needed, then run ./bridge.py reconcile and ./doctor.py."
