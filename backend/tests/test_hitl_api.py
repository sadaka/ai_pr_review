"""M15 demo: pytest backend/tests/test_hitl_api.py -k
"list_pending_returns_queued_rows and approve_posts_review_exactly_once and
reject_marks_dismissed_without_posting and double_resolve_is_idempotent"

Live truth lane (real Tiger Cloud Postgres, same pattern as
test_aggregator_hitl_e2e.py): seeds a low-confidence review through the real
`aggregate()` so it lands in `hitl_reviews` for real, then drives the M15 API
router in-process via ASGITransport. GitHub is faked. Skips cleanly without
TIGER_DATABASE_URL.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest
import pytest_asyncio

from agents.contracts import AgentType, Finding, Severity
from api.app import create_api_app
from hitl.queue import HitlQueue
from integrations.truth_store import TruthStore, create_pool
from orchestrator.nodes import aggregate

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TIGER_DATABASE_URL"),
        reason="requires live TIGER_DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

os.environ.setdefault("API_AUTH_TOKEN", "test-fixture-token")
_HEADERS = {"Authorization": f"Bearer {os.environ['API_AUTH_TOKEN']}"}


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._next_id = 700_000

    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int:
        self._next_id += 1
        self.calls.append(
            {"repo_full_name": repo_full_name, "pr_number": pr_number, "body": body, "event": event}
        )
        return self._next_id

    async def get_pull_request_diff(self, *, repo_full_name: str, pr_number: int) -> str:
        return "diff --git a/x b/x\n--- a/x\n+++ b/x\n+line\n"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool():
    p = await create_pool(os.environ["TIGER_DATABASE_URL"])
    yield p
    await p.close()


async def _queue_low_confidence_review(pool, *, pr_number: int) -> str:
    """Runs the real M5 aggregate() with a low-confidence finding so the
    review is genuinely routed to the HITL queue, not hand-inserted."""
    truth_store = TruthStore(pool)
    hitl = HitlQueue(pool)
    github = FakeGitHub()
    finding = Finding(
        agent_type=AgentType.QUALITY,
        severity=Severity.LOW,
        category="style",
        file="app/main.py",
        line=10,
        confidence=0.2,
        title="minor nit",
        rationale="not a big deal",
    )
    result = await aggregate(
        repo_full_name="test-fixture/m15",
        pr_number=pr_number,
        delivery_id=f"m15-{uuid.uuid4().hex[:12]}",
        findings_by_agent=[[finding]],
        github=github,
        truth_store=truth_store,
        hitl=hitl,
    )
    assert result.hitl_review_id is not None  # sanity: really queued, not auto-posted
    return result.review_id


@pytest_asyncio.fixture
async def review(pool):
    review_id = await _queue_low_confidence_review(pool, pr_number=15)
    yield review_id
    await pool.execute("DELETE FROM hitl_reviews WHERE review_id = $1", review_id)
    await pool.execute("DELETE FROM finding_records WHERE review_id = $1", review_id)
    await pool.execute("DELETE FROM pr_review_records WHERE id = $1", review_id)


def _client(pool, github: FakeGitHub) -> httpx.AsyncClient:
    app = create_api_app(pool, github=github)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://api.test", headers=_HEADERS)


async def test_list_pending_returns_queued_rows(pool, review):
    async with _client(pool, FakeGitHub()) as c:
        resp = await c.get("/api/hitl")
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["review_id"] == review)
    assert row["repo"] == "test-fixture/m15"
    assert row["pr_number"] == 15


async def test_approve_posts_review_exactly_once(pool, review):
    github = FakeGitHub()
    async with _client(pool, github) as c:
        resp = await c.post(f"/api/hitl/{review}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert len(github.calls) == 1
    assert github.calls[0]["repo_full_name"] == "test-fixture/m15"

    # no longer in the pending list
    async with _client(pool, github) as c:
        pending = await c.get("/api/hitl")
    assert review not in {r["review_id"] for r in pending.json()}


async def test_reject_marks_dismissed_without_posting(pool, review):
    github = FakeGitHub()
    async with _client(pool, github) as c:
        resp = await c.post(f"/api/hitl/{review}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"
    assert github.calls == []


async def test_double_resolve_is_idempotent(pool, review):
    github = FakeGitHub()
    async with _client(pool, github) as c:
        first = await c.post(f"/api/hitl/{review}/approve")
        second = await c.post(f"/api/hitl/{review}/approve")
    assert first.status_code == 200
    assert second.status_code == 409
    assert len(github.calls) == 1  # never double-posted


async def test_list_pending_returns_queued_rows_and_approve_posts_review_exactly_once_and_reject_marks_dismissed_without_posting_and_double_resolve_is_idempotent(
    pool,
):
    """Combined-name test so the demo command's AND `-k` filter selects
    something — same M13/M14 precedent for a single-AND-string filter."""
    review_a = await _queue_low_confidence_review(pool, pr_number=151)
    review_b = await _queue_low_confidence_review(pool, pr_number=152)
    try:
        github = FakeGitHub()
        async with _client(pool, github) as c:
            pending = await c.get("/api/hitl")
            assert {review_a, review_b} <= {r["review_id"] for r in pending.json()}

            approved = await c.post(f"/api/hitl/{review_a}/approve")
            assert approved.status_code == 200 and approved.json()["status"] == "resolved"
            assert len(github.calls) == 1

            rejected = await c.post(f"/api/hitl/{review_b}/reject")
            assert rejected.status_code == 200 and rejected.json()["status"] == "dismissed"
            assert len(github.calls) == 1  # reject never posts

            again = await c.post(f"/api/hitl/{review_a}/approve")
            assert again.status_code == 409
            assert len(github.calls) == 1  # never double-posted
    finally:
        for review_id in (review_a, review_b):
            await pool.execute("DELETE FROM hitl_reviews WHERE review_id = $1", review_id)
            await pool.execute("DELETE FROM finding_records WHERE review_id = $1", review_id)
            await pool.execute("DELETE FROM pr_review_records WHERE id = $1", review_id)
