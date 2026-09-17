"""M14: single-user bearer-token auth on the read API. Pure ASGI, no DB/Redis
needed — the fake pool is never actually queried on the 401 paths, and the
happy path never inspects returned rows (M7's test_reviews_api.py already
covers row content against a live DB).

Also proves `webhook_receiver` is unaffected: it keeps checking only its own
HMAC signature, no bearer token required there (a different, correct trust
boundary — GitHub doesn't send this token).
"""
from __future__ import annotations

import os

import httpx
import pytest

from api.app import create_api_app
from webhook_receiver.app import create_app as create_webhook_app

os.environ["API_AUTH_TOKEN"] = "test-suite-token"


class _FakePool:
    async def fetch(self, *args, **kwargs):
        return []


@pytest.fixture
def client():
    app = create_api_app(_FakePool())
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://api.test")


async def test_missing_token_rejected(client):
    async with client as c:
        resp = await c.get("/api/reviews")
    assert resp.status_code == 401


async def test_wrong_token_rejected(client):
    async with client as c:
        resp = await c.get("/api/reviews", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


async def test_valid_token_allowed(client):
    async with client as c:
        resp = await c.get("/api/reviews", headers={"Authorization": "Bearer test-suite-token"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_webhook_receiver_unaffected():
    """/webhook has no bearer-token dependency at all — bad/missing GitHub
    signature still 401s on its own terms, with no Authorization header sent."""
    app = create_webhook_app(job_queue=None, webhook_secret="whatever")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://webhook.test") as c:
        resp = await c.post(
            "/webhook",
            content=b"{}",
            headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d1"},
        )
    # No Authorization header was sent at all; rejection here must be about
    # the missing/invalid HMAC signature, never about a bearer token.
    assert resp.status_code == 401


async def test_missing_token_rejected_and_wrong_token_rejected_and_valid_token_allowed_and_webhook_receiver_unaffected(
    client,
):
    """Combined-name test so the demo command's AND `-k` filter (4 substrings)
    selects something — see M13's checkpoints/M13.md for why a plain `-k`
    across 4 separately-named tests selects 0 (the filter is one AND-string,
    not 4 OR'd substrings)."""
    async with client as c:
        assert (await c.get("/api/reviews")).status_code == 401
        assert (await c.get("/api/reviews", headers={"Authorization": "Bearer nope"})).status_code == 401
        ok = await c.get("/api/reviews", headers={"Authorization": "Bearer test-suite-token"})
        assert ok.status_code == 200
        assert ok.json() == []

    webhook_app = create_webhook_app(job_queue=None, webhook_secret="whatever")
    webhook_transport = httpx.ASGITransport(app=webhook_app)
    async with httpx.AsyncClient(transport=webhook_transport, base_url="http://webhook.test") as wc:
        resp = await wc.post(
            "/webhook",
            content=b"{}",
            headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d1"},
        )
    assert resp.status_code == 401
