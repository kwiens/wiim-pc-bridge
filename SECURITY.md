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

Soloist currently accepts its API key only as a command-line option. The key is
therefore visible inside the container's process table. Treat membership in the
host's Docker group as root-equivalent and do not grant untrusted users Docker
access.

WiiM's local control API presents a self-signed HTTPS certificate. The bridge
therefore cannot validate a public certificate chain. Configuration validation
restricts both device destinations to literal IPv4 addresses inside the
explicitly trusted LAN configured by `TRUSTED_NETWORK`; do not set that network
broader than necessary.
