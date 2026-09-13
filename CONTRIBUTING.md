# Contributing

Keep changes portable and fail closed around WiiM group topology. A change must
never select a follower independently or silently alter native group membership.

Before submitting a change, run:

```bash
python3 -m pip install --requirement requirements-dev.txt
python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
bandit -q -r bridge.py bridge_config.py doctor.py render-config.py \
  volume_handoff.py scripts/check-secrets.py
yamllint .github compose.yaml
docker compose --env-file .env.example config --quiet
docker run --rm -v "$PWD:/mnt:ro" koalaman/shellcheck:v0.11.0 \
  /mnt/ensure-runtime.sh /mnt/install-service.sh /mnt/save-soloist-key.sh \
  /mnt/soloist-entrypoint.sh /mnt/update-soloist.sh
docker run --rm -i hadolint/hadolint:v2.15.1-alpine < Dockerfile.soloist
./scripts/check-secrets.py
```

Do not include `.env`, credentials, cache databases, device identifiers, or
private network details in commits, bug reports, or logs.
