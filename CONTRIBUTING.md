# Contributing

Keep changes portable and fail closed around WiiM group topology. A change must
never select a follower independently or silently alter native group membership.

Before submitting a change, run:

```bash
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example config --quiet
shellcheck *.sh
./scripts/check-secrets.py
```

Do not include `.env`, credentials, cache databases, device identifiers, or
private network details in commits, bug reports, or logs.
