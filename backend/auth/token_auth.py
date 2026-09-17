"""Single-user bearer-token auth for the read API + HITL actions (M14).

Closes the RBAC/auth item `security/threat_model.md` explicitly deferred:
"no authenticated surface yet ... revisit when it is exposed" (M13 shipped the
first real exposed surface). Deliberately NOT wired onto `webhook_receiver` —
GitHub's HMAC signature (`webhook_receiver/signature.py`) is that endpoint's
own, correct trust boundary; requiring this token there too would be a
regression (GitHub doesn't send it), not added security.

Single user, single static token: compared in constant time against
`API_AUTH_TOKEN`, mirroring the same `hmac.compare_digest` pattern the webhook
signature check already uses.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def _expected_token() -> str:
    token = os.environ.get("API_AUTH_TOKEN")
    if not token:
        raise RuntimeError("API_AUTH_TOKEN is not set")
    return token


def check_token(provided: str | None, expected: str) -> bool:
    """Pure predicate, unit-testable without touching env/FastAPI wiring."""
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


async def require_auth(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: raises 401 unless `Authorization: Bearer <token>`
    matches `API_AUTH_TOKEN`. Reads the env var per-call (not at import time)
    so tests can monkeypatch it freely."""
    provided: str | None = None
    if authorization and authorization.startswith("Bearer "):
        provided = authorization.removeprefix("Bearer ")

    if not check_token(provided, _expected_token()):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
