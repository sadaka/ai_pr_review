"""M7: GET /api/reviews against the live Tiger truth lane, driven in-process
via ASGITransport (no server). Skips cleanly without TIGER_DATABASE_URL.

Inserts one review + two costed agent_events, asserts the endpoint returns the
row with summed cost, then deletes its own rows.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest
import pytest_asyncio

from api.app import create_api_app, create_pool

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TIGER_DATABASE_URL"),
        reason="requires live TIGER_DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool():
    p = await create_pool(os.environ["TIGER_DATABASE_URL"])
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="module")
async def client(pool):
    app = create_api_app(pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        yield c


async def test_lists_review_with_summed_cost(client, pool):
    review_id = str(uuid.uuid4())
    delivery_id = f"m7-{uuid.uuid4().hex[:12]}"
    await pool.execute(
        """
        INSERT INTO pr_review_records (id, repo, pr_number, github_delivery_id, status, overall_confidence)
        VALUES ($1, 'test-fixture/m7', 7, $2, 'posted', 0.812)
        """,
        review_id,
        delivery_id,
    )
    for cost in (0.012, 0.008):
        await pool.execute(
            """
            INSERT INTO agent_events (ts, review_id, agent, event_type, cost_usd)
            VALUES (now(), $1, 'security', 'llm.call', $2)
            """,
            review_id,
            cost,
        )

    try:
        resp = await client.get("/api/reviews")
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["id"] == review_id)
        assert row["repo"] == "test-fixture/m7"
        assert row["pr_number"] == 7
        assert row["status"] == "posted"
        assert row["confidence"] == pytest.approx(0.812, abs=1e-3)
        assert row["cost_usd"] == pytest.approx(0.020, abs=1e-6)
    finally:
        await pool.execute("DELETE FROM agent_events WHERE review_id = $1", review_id)
        await pool.execute("DELETE FROM pr_review_records WHERE id = $1", review_id)


async def test_empty_when_no_matching_rows(client):
    resp = await client.get("/api/reviews")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
