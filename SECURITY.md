# Security

Do not open a public issue containing API keys, account details, private IP
addresses, logs with credentials, or other sensitive configuration.

Before publishing a fork or changing this repository from private to public:

1. Run `./scripts/check-secrets.py`.
2. Review the complete Git history, not just the current working tree.
3. Confirm `.env`, `.secrets/`, `cache/`, `runtime/`, and the PCM FIFO are absent.
4. Rotate any credential that has ever appeared in a commit, issue, or chat.

This project passes a Spotify Soloist API key to the Soloist process. It stores
that key only in a mode-600 file outside Git and exposes it to the container as
a Docker Compose secret.
