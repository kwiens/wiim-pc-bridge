# PC + WiiM Spotify bridge

This project creates one Spotify Connect target that plays in sync through a
Linux PC and an existing native WiiM multi-room group.

```text
Spotify -> Soloist -> private PipeWire sink -> PCM FIFO -> OwnTone
                                                       |       |
                                                       |       +-> WiiM leader
                                                       |                |
                                                       |                +-> native WiiM followers
                                                       +-> Shairport -> PC audio sink
```

OwnTone sends exactly one AirPlay 2 stream to the configured WiiM leader. The
leader distributes that stream to its existing followers using WiiM MRM. The PC
receives a separately timestamped classic AirPlay stream through Shairport Sync.
The bridge never makes the PC a native WiiM follower and never selects a WiiM
follower independently.

All paths are buffered for synchronized music. This is not intended for games
or lip-synced video.

## Requirements

- Linux with PipeWire's PulseAudio compatibility service
- Docker Engine with Docker Compose v2
- Python 3.10 or newer
- Spotify Premium and a Spotify Soloist developer API key
- One WiiM leader with its native follower group already configured
- DHCP reservations for the WiiM addresses used in `.env`

The current images target x86-64 because the official Soloist archive in the
Dockerfile is the x86-64 build.

## Install

Clone the repository, then create the local configuration:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` for the current user, PipeWire sink, WiiM addresses and names. The
file is intentionally ignored by Git. Save the Soloist key without displaying
it in the terminal or shell history:

```bash
./save-soloist-key.sh
```

Prepare and start the bridge:

```bash
./ensure-runtime.sh
docker compose up -d --build
./bridge.py reconcile --wait-seconds 180
./doctor.py
```

Install the relocatable user service so the bridge starts after boot:

```bash
./install-service.sh
loginctl enable-linger "$USER"
```

`install-service.sh` renders a machine-local unit containing the clone's
absolute path. Moving the repository later requires running the installer again.

## Normal use

Select the configured bridge name (default: **PC + WiiM**) in Spotify and play
normally. Useful controls are:

```bash
./bridge.py status
./bridge.py group-status
./bridge.py reconcile
./bridge.py select-all --confirm-wiim-takeover
./bridge.py select-local
./bridge.py set-volume local 100
./bridge.py set-volume wiim 50
./bridge.py set-offset local 125
./bridge.py stop
./doctor.py
```

`reconcile` verifies the native WiiM topology before restoring the two selected
outputs, their volumes, and their sync offsets from `.env`. The boot service
waits up to three minutes for OwnTone and the WiiMs, then runs this command.

`select-all` also fails closed unless the configured leader and follower
topology is healthy. Its confirmation flag acknowledges that starting AirPlay
will replace whatever source is currently active on the WiiM leader.

`stop` deselects OwnTone outputs without changing native WiiM group membership.
Positive offsets delay that output; valid offsets are -2000 through 2000 ms.

## Configuration

`.env.example` documents every host-specific value. The most important are:

| Setting | Purpose |
| --- | --- |
| `DISPLAYPORT_SINK` | Stable PipeWire sink used by the local Shairport stream |
| `KITCHEN_IP`, `LIVING_ROOM_IP` | Reserved addresses used for fail-closed topology checks |
| `KITCHEN_DEVICE_NAME` | AirPlay service name advertised by the WiiM leader |
| `LOCAL_OUTPUT_NAME`, `WIIM_OUTPUT_NAME` | Exact OwnTone output identities |
| `LOCAL_VOLUME`, `WIIM_VOLUME` | Levels restored by reconciliation |
| `LOCAL_OFFSET_MS`, `WIIM_OFFSET_MS` | Per-output synchronization adjustments |

`ensure-runtime.sh` renders `runtime/owntone.conf` from `owntone.conf.in` on
every start. Runtime state and configuration are not committed.

The Soloist key is authoritative at the path configured by
`SOLOIST_KEY_FILE`. An ignored, mode-600 project backup is maintained at
`.secrets/soloist_api_key`. Neither file belongs in Git or the Docker build
context.

## Reliability model

- Image bases and third-party images are pinned by digest.
- Soloist is forced to a private `wiim_bridge` PipeWire sink.
- PCM capture uses 44.1 kHz signed 16-bit stereo and a tested 100 ms fragment.
- OwnTone uses a configurable 2250 ms startup buffer by default.
- The Soloist entrypoint supervises both Soloist and `parec`; either child
  exiting restarts the container.
- Shairport uses classic AirPlay to avoid competing with OwnTone for AirPlay 2
  PTP services. Its Pulse backend shares the selected PC sink with desktop audio.
- All containers use `unless-stopped`; systemd waits for PipeWire and Docker.
- Startup reconciliation restores output selection, volume and offsets rather
  than relying only on OwnTone's cache database.
- `doctor.py` audits secrets, containers, startup, audio routing, output state,
  WiiM topology, and Soloist expiry without changing the system.

## Updates

Official Soloist development builds expire after roughly 90 days. Check the
official download without changing the installation:

```bash
./update-soloist.sh --check
```

Install a newer build when available:

```bash
./update-soloist.sh --apply
```

The updater calculates the archive SHA-256, updates the Dockerfile and local
image tag, rebuilds Soloist, and recreates only that container. Review and
commit those pin changes after `./doctor.py` passes.

Dependabot checks Docker and GitHub Actions dependencies monthly. CI validates
the Python tests, syntax, Compose model, shell scripts, and credential scan.

## Recovery on a replacement machine

1. Install the prerequisites and clone the private repository.
2. Restore `.env` from a secure backup or recreate it from `.env.example`.
3. Generate a fresh Soloist key and run `./save-soloist-key.sh`.
4. Run `./ensure-runtime.sh` and `docker compose up -d --build`.
5. Run `./install-service.sh`, enable user lingering, and reboot once.
6. Select the bridge in Spotify and run `./doctor.py`.

The ignored OwnTone and Soloist caches are optional. A clean machine can rebuild
them; the committed configuration and reconciliation command restore the
important behavior.

## Diagnostics and development

```bash
docker compose ps
docker compose logs --tail 150 owntone shairport soloist
systemctl --user status wiim-pc-bridge.service
journalctl --user -u wiim-pc-bridge.service
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example config --quiet
./scripts/check-secrets.py
```

OwnTone's local web interface is available at <http://127.0.0.1:3689>.

See [SECURITY.md](SECURITY.md) before making a fork public. No license has been
selected yet; choose one deliberately before publishing this as open source.
