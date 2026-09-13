# Changelog

## Unreleased

- Preserve the active Spotify volume when playback moves to the PC + WiiM
  Connect target and retain the last stable level across bridge restarts.

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
