# Security

## Reporting a vulnerability

Report privately through GitHub's **Report a vulnerability** button on the
[Security tab](../../security/advisories/new). That opens a private advisory
visible only to the maintainers.

> Maintainers: private vulnerability reporting must be switched on in
> *Settings → Code security* before that link works. Until it is, the button is
> absent and reporters have no private channel.

Please do not open a public issue containing API keys, account details, private
IP addresses, logs with credentials, or other sensitive configuration. Expect a
first response within a week.

## Before making this repository public

1. Run `./scripts/check-secrets.py`. It scans the working tree *and* every blob
   reachable from any ref, because publishing a repository publishes its whole
   history — a key committed once and deleted later is still in every clone.
   It also greps for the literal key this installation uses, so a credential
   whose format differs from the built-in patterns is still caught.
2. Confirm `.env`, `cache/`, `runtime/`, and the PCM FIFO are absent from the
   index.
3. Rotate any credential that has ever appeared in a commit, issue, or chat.
   Rewriting history does not help once a clone exists.

## Threat model

This project passes a Spotify Soloist API key to the Soloist process. It stores
that key only in a mode-600 file outside the repository; configuration
validation rejects a `SOLOIST_KEY_FILE` inside the clone so it cannot be
committed.

Soloist accepts its API key only as a command-line option, so the key is
visible in the process table. Container processes are visible in the host's
`/proc`, which means **any local user on this machine can read the key**, not
only members of the `docker` group. Do not run this on a multi-user host.
Treat membership in the host's Docker group as root-equivalent.

All three containers use host networking. OwnTone's HTTP and DAAP interfaces
therefore listen on every interface and are gated only by `TRUSTED_NETWORK`,
which OwnTone matches as a dotted-octet prefix. Configuration validation
requires that value to be a private `/8`, `/16` or `/24` with no host bits set,
so a typo cannot silently widen it to the whole internet. OwnTone's MPD control
server has no authentication at all and is disabled in the rendered
configuration.

WiiM's local control API presents a self-signed HTTPS certificate, so the
bridge cannot validate a public certificate chain. Validation restricts every
device destination to a literal IPv4 address inside `TRUSTED_NETWORK`; that
containment is what justifies the disabled certificate check.

`update-soloist.sh --check` downloads the official archive but never executes
it. On `--apply`, the new binary runs only inside the built image, after the
Dockerfile has verified the archive against the pinned SHA-256.
