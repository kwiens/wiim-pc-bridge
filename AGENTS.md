# Incident reports for audio faults

For every reported or observed audio fault in this project, create or update an
incident report while diagnosing it. Do this even if the cause or fix is not yet
known. Prioritize restoring audio; the report must not delay an urgent repair.

Keep installation-specific reports under `runtime/incidents/` (gitignored).
Start from `docs/incident-report-template.md` and update the same report as new
evidence arrives. Capture the time and timezone, symptoms and impact, the
relevant configuration and observed system state before intervention, bounded
log/measurement evidence, actions taken, verification, and remaining unknowns.
Mark unverified hypotheses as such and record observation gaps. In particular,
digital PCM or a WiiM `play` flag does not verify audible speaker output.

Never put credentials, full Soloist process arguments, host identities, device
identifiers, private addresses, or raw local diagnostics in tracked files or
public issues. A public postmortem must be deliberately sanitized and reviewed
before it is added to the repository. If the issue cannot be fully resolved,
leave the incident open with a concrete next check instead of claiming a fix.
