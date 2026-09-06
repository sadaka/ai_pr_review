"""GitHub webhook HMAC-SHA256 signature verification.

GitHub signs the raw request body with the App's webhook secret and sends the
result as `X-Hub-Signature-256: sha256=<hexdigest>`. We must verify against the
RAW bytes (not a re-serialized JSON dict — re-serialization can change byte
layout and break the signature) and compare in constant time.
"""
from __future__ import annotations

import hashlib
import hmac


def verify_signature(payload_body: bytes, secret: str, signature_header: str | None) -> bool:
    """Return True iff signature_header is a valid sha256= HMAC of payload_body."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)
