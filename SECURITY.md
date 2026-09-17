# Security Policy

`ai-pr-review` processes untrusted input by design — every PR diff and every
chunk of indexed source code it reasons over can be authored by someone you
don't trust. The full threat model (assets, trust boundaries, prompt-injection
threats, and mitigations) lives at
[`backend/security/threat_model.md`](backend/security/threat_model.md) —
read that first if you're evaluating this project's security posture or
looking for what's already covered vs. deliberately deferred.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for a security vulnerability.

Instead, use GitHub's private vulnerability reporting for this repo
(**Security** tab → **Report a vulnerability**), or email the maintainer
directly. Include:

- A description of the issue and its potential impact
- Steps to reproduce (a minimal PR diff or webhook payload, if applicable)
- Any suggested mitigation, if you have one

You should expect an initial response within a few days. This is a
single-maintainer project — please be patient, and thank you for reporting
responsibly.

## Scope

In scope:
- The webhook receiver, orchestrator, specialist agents, and API surface in
  this repository
- The prompt-injection guard (`backend/security/injection_guard.py`) and its
  bypasses
- Auth/HITL flows in `backend/auth/` and `backend/api/hitl.py`

Out of scope / already known and tracked (see the threat model and README's
TODO list):
- Lack of RBAC / multi-tenant isolation — this is currently a single-operator
  tool by design
- Lack of webhook rate-limiting
- Third-party service outages (GitHub, Tiger Cloud, OpenAI)

## Supported versions

This project does not yet maintain multiple release branches. Security fixes
land on `main`; there is no LTS or backport policy at this stage.
