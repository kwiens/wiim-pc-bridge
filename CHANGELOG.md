# Changelog

## Unreleased

### Fixed

- Wait up to 60 seconds for WirePlumber to enumerate the configured DisplayPort
  sink during boot instead of permanently failing when PipeWire's socket becomes
  ready first.
- Cold-start the Compose stack on every bridge-service start. Docker can restore
  `unless-stopped` containers before PipeWire and the user service, leaving
  OwnTone and the PCM FIFO individually healthy but connected in a stale order.
  The service now replaces that unordered state before reconciling outputs.
- Verify active PCM end to end after service startup. When Spotify is producing
  audio but the physical PC sink remains digitally silent after both source and
  output grace periods, startup now fails and uses the clean recovery path.
- Repeat that flow check during active playback and automatically cold-recover
  after two consecutive confirmed stalls, while treating genuine source silence
  and a temporarily unavailable diagnostic as non-failures.

### Added

- Opt-in idle speaker keepalive: a level-configurable, cosine-faded three-second
  tone after ten minutes without audio, mixed independently of Spotify volume.
  A separate user service avoids restarting the bridge containers. Safety checks
  skip disconnected outputs, other active sources, and uncertain device state;
  the helper never reconnects AirPlay or changes playback volumes.

### Security

- Scan the whole Git history for credentials, not just the working tree. A key
  committed once and deleted later was previously reported as clean while every
  clone still carried it. The scan also matches the installation's literal key,
  so a credential whose format differs from the built-in patterns is caught,
  and the private-key pattern now covers the ENCRYPTED, DSA and PGP headers.
- Disable OwnTone's MPD control server, which has no authentication and was
  reachable from the whole LAN because the containers use host networking.
- Reject a `SOLOIST_KEY_FILE` inside the repository, where `git add -A` would
  publish it, and drop the redundant `.secrets/` copy of the key entirely.
- Require `TRUSTED_NETWORK` to be a private network with no host bits set. A
  value such as `192.0.2.10/0` was silently widened to `0.0.0.0/0`, which made
  the "speakers are inside the trusted LAN" check accept any address and
  allowed certificate-unverified requests to arbitrary hosts.
- `update-soloist.sh --check` no longer downloads and executes an unsigned
  binary on the host before verifying it. On `--apply` the new build runs only
  inside the image, after the Dockerfile has checked it against the pin.

### Fixed

- Load configuration lazily instead of at import. A fresh clone has no `.env`,
  so every documented command — including the test suite and `doctor.py`, whose
  job is diagnosing a broken install — aborted with a raw traceback.
- Convert `TRUSTED_NETWORK` to the dotted-octet prefix OwnTone actually
  matches. A CIDR value was written through verbatim and never matched any
  client, so the documented LAN trust exemption silently did nothing.
- Report `systemctl`/`loginctl` state instead of raising on it. Their non-zero
  exit *is* the answer, so every unhealthy state surfaced as a subprocess error
  and the restart-policy check below it never ran.
- Fail, not warn, when an output outside the configured pair is selected, and
  detect it by identity rather than by matching one configured device name.
  `doctor.py` exited 0 while a follower was independently selected.
- Keep waiting for Soloist after a trapped signal interrupts `wait`. PID 1
  exited within milliseconds of forwarding SIGTERM, so the kernel killed
  Soloist mid-flush and `stop_grace_period` was never actually used.
- Honour a stop signal that arrives before the children exist, rather than
  ignoring it and starting them anyway.
- Wait for a reader on the PCM FIFO before reporting the capture as started.
  Until OwnTone opens the pipe the recorded PID is a blocked shell, so the
  health check went green with no audio flowing.
- Write `runtime/owntone.conf` through its existing inode. The atomic rename
  swapped the inode out from under the bind mount, so a running container never
  saw a re-rendered configuration.
- Substitute OwnTone template placeholders in one pass, so a value containing
  `@NAME@` is neither re-expanded nor treated as an unresolved placeholder.
- Wrap errors raised while reading an HTTP response body. `urllib` does not
  wrap those in `URLError`, so a stall mid-body escaped as a traceback.
- Deselect outputs before applying levels in `reconcile`, which is what
  actually prevents a cached-volume burst.
- Stop the `.env` file from meaning three different things. The shell helpers
  now read values through the single strict parser rather than sourcing the
  file, and the parser handles inline comments the way Compose does.
- Reject `%`, `$`, `'` and `;` in the project path before writing it into a
  systemd unit, where they are silently reinterpreted.
- Only restart the service during rollback if the installer actually restarted
  it, so a failure that never touched a healthy bridge cannot stop it.
- Keep `update-soloist.sh`'s rollback going after a failed restore instead of
  aborting under `set -e` having already announced success.
- Do not `chmod 0700` a key directory the helper did not create; a key path
  directly in `$HOME` relocked the home directory.
- Match PipeWire sink indexes exactly. Sink 550 satisfied a check for sink 55.
- Report a container with no health check as such rather than as healthy.

### Changed

- Support any number of WiiM followers through the optional `WIIM_FOLLOWERS`
  setting. The group is judged by comparing the leader's reported followers
  against the configured set, so a group with two followers is usable and a
  mismatch names what is missing and what is unexpected. The old
  `slaves == 1` check rejected every other topology permanently.
- Accept Compose's own `COMPOSE_*`/`DOCKER_*` keys in `.env`, and suggest the
  closest known key when an unrecognised one appears.
- Report configured values in diagnostics instead of hard-coded hardware names.
- Treat an uninstalled boot service as a warning, not a failure.
- Require `BRIDGE_UID`, `BRIDGE_GID` and `BRIDGE_RUNTIME_DIR` in Compose rather
  than defaulting to 1000, which disagreed with the Python tools' `os.getuid()`.

### Tests

- Rewrite the suite so it isolates itself from the developer's environment and
  uses literal fixtures: assertions built from `CONFIG` compared the
  configuration with itself and could not fail. Sixteen mutations that
  previously passed unnoticed — including deleting reconcile's verification
  block and the follower-identity check — are now caught.


## Unreleased

- Preserve the active Spotify volume when playback moves to the PC + WiiM
  Connect target and retain the last stable level across bridge restarts.
- License the project under MIT and broaden the architecture and use-case
  documentation beyond the reference PC + WiiM deployment.

## v1.0.1 - 2026-09-13

- Validate configuration structure, paths, volumes, offsets, buffer bounds, and
  WiiM membership in the trusted LAN.
- Apply output levels before connecting, preventing stale cached volume bursts.
- Detect duplicate or extra selected OwnTone outputs in the health audit.
- Roll back Soloist manifest pins and the running container after a failed
  update.
- Wait for every container health check during systemd startup.
- Add Ruff, Bandit, and YAML validation to CI.

## v1.0.0 - 2026-09-13

- Route Spotify Soloist through an isolated PipeWire sink and supervised PCM
  capture into OwnTone.
- Synchronize a classic AirPlay PC receiver with one AirPlay 2 stream to the
  native WiiM group leader.
- Preserve and validate the Kitchen leader / Living Room follower topology.
- Share the PC DisplayPort sink with desktop audio through PipeWire.
- Restore output selection, volumes, and offsets during startup reconciliation.
- Add a relocatable user service, full health audit, update helper, tests, CI,
  credential scanning, and recovery documentation.
