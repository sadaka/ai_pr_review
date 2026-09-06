from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Loaded once at collection time so live-credential tests (test_specialists_e2e.py)
# can read TIGER_DATABASE_URL / OPENAI_API_KEY from backend/.env without every
# test needing to source it manually. Harmless no-op for tests that don't use
# either (webhook/job-queue/orchestrator tests never read these env vars).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TEST_SECRET = "test-webhook-secret"


def sign(body: bytes, secret: str = TEST_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def pull_request_payload(*, action: str = "opened", number: int = 42, repo: str = "octocat/hello-world") -> bytes:
    return json.dumps(
        {
            "action": action,
            "number": number,
            "pull_request": {"number": number, "head": {"sha": "abc123"}},
            "repository": {"full_name": repo},
        }
    ).encode()


@pytest.fixture
def webhook_secret() -> str:
    return TEST_SECRET
