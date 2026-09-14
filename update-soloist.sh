#!/bin/sh
set -eu

project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
archive_url=https://soloist-builds.spotifycdn.com/soloist_release_x86_64.tar.gz
candidate_image=wiim-pc-bridge/soloist:candidate
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
  # Never let a failing restore abort the rest of the rollback: `set -e` would
  # stop here having already announced that the pins were restored.
  set +e
  if [ "$rollback_needed" -eq 1 ]; then
    echo "Soloist update failed; restoring the previous pins." >&2
    restored=1
    cp "$temporary_directory/Dockerfile.soloist.previous" \
      "$project_directory/Dockerfile.soloist" || restored=0
    cp "$temporary_directory/compose.yaml.previous" \
      "$project_directory/compose.yaml" || restored=0
    if [ "$restored" -eq 0 ]; then
      echo "Could not restore the manifest files. Recover them with" \
        "'git checkout -- Dockerfile.soloist compose.yaml'." >&2
    fi
    if [ "$container_changed" -eq 1 ]; then
      (
        cd "$project_directory" || exit 1
        docker compose up -d --no-deps --force-recreate \
          --wait --wait-timeout 180 soloist
      ) || echo "Automatic container rollback failed; run" \
        "'docker compose up -d --build soloist'." >&2
    fi
  fi
  docker image rm "$candidate_image" >/dev/null 2>&1
  rm -rf "$temporary_directory"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

current_sha=$(sed -nE 's/^ARG SOLOIST_ARCHIVE_SHA256=([0-9a-f]{64})$/\1/p' \
  "$project_directory/Dockerfile.soloist")
current_tag=$(sed -nE 's#^    image: wiim-pc-bridge/soloist:(.+)$#\1#p' \
  "$project_directory/compose.yaml")
if [ -z "$current_sha" ] || [ -z "$current_tag" ]; then
  echo "Could not read the current Soloist pins; no files were changed." >&2
  exit 1
fi

archive="$temporary_directory/soloist.tar.gz"
curl --fail --silent --show-error --location "$archive_url" --output "$archive"
archive_sha=$(sha256sum "$archive" | awk '{print $1}')

printf 'Current archive SHA-256:  %s\n' "$current_sha"
printf 'Downloaded SHA-256:       %s\n' "$archive_sha"

if [ "$archive_sha" = "$current_sha" ]; then
  echo "The pinned Soloist build is current."
  exit 0
fi

if [ "$action" = --check ]; then
  # Deliberately no execution and no extraction here. The download is an
  # unsigned artifact from a CDN; running it to read its version would make
  # this read-only check a code-execution path on the host.
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
updated_sha=$(sed -nE 's/^ARG SOLOIST_ARCHIVE_SHA256=([0-9a-f]{64})$/\1/p' \
  "$project_directory/Dockerfile.soloist")
if [ "$updated_sha" != "$archive_sha" ]; then
  echo "Could not update the Soloist archive pin." >&2
  exit 1
fi

# Build first. The Dockerfile re-downloads the archive and checks it against the
# pin just written, so the build is the integrity gate, and the new binary only
# ever runs inside the image -- never on the host.
cd "$project_directory"
docker build --file Dockerfile.soloist --tag "$candidate_image" .

version_output=$(docker run --rm --entrypoint /usr/local/bin/soloist \
  "$candidate_image" --version)
version_number=$(printf '%s\n' "$version_output" | sed -nE 's/^soloist ([0-9.]+).*/\1/p')
build_date=$(printf '%s\n' "$version_output" | sed -nE 's/.*\(([0-9]{8})\).*/\1/p')
if [ -z "$version_number" ] || [ -z "$build_date" ]; then
  echo "Could not parse the built Soloist version: $version_output" >&2
  exit 1
fi

candidate_tag="$version_number-$build_date"
expiry_date=$(date -d "${build_date} +90 days" +%F)
printf 'Built Soloist:            %s\n' "$version_output"
printf 'Approximate expiry date:  %s\n' "$expiry_date"

if [ "$candidate_tag" = "$current_tag" ]; then
  echo "The archive changed without a new version/build identifier; refusing it." >&2
  exit 1
fi

sed -i -E \
  "s#^    image: wiim-pc-bridge/soloist:.*#    image: wiim-pc-bridge/soloist:$candidate_tag#" \
  "$project_directory/compose.yaml"
updated_tag=$(sed -nE 's#^    image: wiim-pc-bridge/soloist:(.+)$#\1#p' \
  "$project_directory/compose.yaml")
if [ "$updated_tag" != "$candidate_tag" ]; then
  echo "Could not update the Soloist image tag." >&2
  exit 1
fi

docker tag "$candidate_image" "wiim-pc-bridge/soloist:$candidate_tag"
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
