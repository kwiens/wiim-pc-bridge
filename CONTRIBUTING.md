# Contributing

Keep changes portable and fail closed around WiiM group topology. A change must
never select a follower independently or silently alter native group
membership.

The test suite runs on a fresh clone with no `.env`: configuration is loaded
lazily and the tests pin their own temporary configuration, so nothing depends
on the machine you happen to be on. If a change makes `python3 -m unittest
discover -s tests` require a local `.env`, that is a bug.

Before submitting a change, run the same checks CI runs:

```bash
python3 -m pip install --requirement requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 -m compileall -q bridge.py bridge_config.py doctor.py render-config.py \
  volume_handoff.py idle_keepalive.py audio_flow_check.py bridge_supervisor.py \
  output_control.py startup_reconcile.py capture_relay.py tests
ruff check .
ruff format --check .
bandit -q -r bridge.py bridge_config.py doctor.py render-config.py \
  volume_handoff.py idle_keepalive.py audio_flow_check.py bridge_supervisor.py \
  output_control.py startup_reconcile.py capture_relay.py \
  scripts/check-secrets.py scripts/verify-recovery.py
yamllint .github compose.yaml
docker compose --env-file .env.example config --quiet
docker run --rm -i \
  hadolint/hadolint@sha256:a1d49ae1a4e83c1dbad26b8c1ad7588c8bd1e04f4866b34ad3cac50335198552 \
  < Dockerfile.soloist
docker run --rm -i \
  hadolint/hadolint@sha256:a1d49ae1a4e83c1dbad26b8c1ad7588c8bd1e04f4866b34ad3cac50335198552 \
  < Dockerfile.capture
docker run --rm -v "$PWD:/mnt:ro" \
  koalaman/shellcheck@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d \
  /mnt/ensure-runtime.sh /mnt/install-service.sh /mnt/save-soloist-key.sh \
  /mnt/soloist-entrypoint.sh /mnt/update-soloist.sh /mnt/capture-entrypoint.sh
./scripts/check-secrets.py
```

The linter images are pinned by digest to the same versions CI uses, so a local
pass and a CI pass mean the same thing.

## Tests

New behaviour needs a test that fails without the change. Two habits make that
real here:

- Build fixtures from literal values, not from `CONFIG`. A fixture read back
  from the configuration the code also reads makes the assertion a tautology
  that no code change can falsify.
- Compare `doctor.failures` and `doctor.warnings` exactly rather than asserting
  a substring appears somewhere in them, so a spurious extra report fails.

`tests/support.py` provides `configured(...)`, which writes a temporary `.env`,
clears the ambient environment of every known key, and resets the config cache.
It also isolates output policy and lock files from the live installation.

The supervisor suite includes multi-step failures through a real HTTP simulator.
Use the guarded live test helper described in README only with the operator's
permission. Record idle and active-playback tests separately. A unit-test count
or an idle Docker health check is not evidence of audible end-to-end recovery.

Do not include `.env`, credentials, cache databases, device identifiers, or
private network details in commits, bug reports, or logs. `./doctor.py` output
contains your sink names, device names, and LAN addresses — redact it before
pasting it into an issue.
