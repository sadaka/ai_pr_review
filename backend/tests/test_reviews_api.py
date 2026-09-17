"""M7: GET /api/reviews against the live Tiger truth lane, driven in-process
via ASGITransport (no server). Skips cleanly without TIGER_DATABASE_URL.

Inserts one review + two costed agent_events, asserts the endpoint returns the
row with summed cost, then deletes its own rows.

M16 adds GET /api/reviews/{id} — same pattern, plus a finding_records row to
prove the detail endpoint returns findings.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest
import pytest_asyncio

from api.app import create_api_app, create_pool


class FakeGitHub:
    """Only `get_pull_request_diff` matters here — M16's diff-stat lookup."""

    def __init__(self, diff_text: str = "") -> None:
        self.diff_text = diff_text
        self.calls: list[dict] = []

    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int:
        raise NotImplementedError("not exercised by these tests")

    async def get_pull_request_diff(self, *, repo_full_name: str, pr_number: int) -> str:
        self.calls.append({"repo_full_name": repo_full_name, "pr_number": pr_number})
        return self.diff_text

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


_SAMPLE_DIFF = (
    "diff --git a/app/db.py b/app/db.py\n"
    "--- a/app/db.py\n"
    "+++ b/app/db.py\n"
    "+new line one\n"
    "+new line two\n"
    "-old line\n"
)


@pytest_asyncio.fixture(loop_scope="module")
async def client(pool):
    os.environ.setdefault("API_AUTH_TOKEN", "test-fixture-token")
    app = create_api_app(pool, github=FakeGitHub(_SAMPLE_DIFF))
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {os.environ['API_AUTH_TOKEN']}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test", headers=headers) as c:
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


async def test_review_detail_includes_findings(client, pool):
    review_id = str(uuid.uuid4())
    delivery_id = f"m16-{uuid.uuid4().hex[:12]}"
    await pool.execute(
        """
        INSERT INTO pr_review_records (id, repo, pr_number, github_delivery_id, status, overall_confidence)
        VALUES ($1, 'test-fixture/m16', 16, $2, 'posted', 0.5)
        """,
        review_id,
        delivery_id,
    )
    await pool.execute(
        """
        INSERT INTO finding_records
            (review_id, agent_type, severity, category, summary, file_path, line_start, confidence, rationale)
        VALUES ($1, 'security', 'high', 'sql-injection', 'possible SQLi', 'app/db.py', 42, 0.9, 'unparameterized query')
        """,
        review_id,
    )

    try:
        resp = await client.get(f"/api/reviews/{review_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == review_id
        assert body["repo"] == "test-fixture/m16"
        assert len(body["findings"]) == 1
        assert body["diff_stats"] == {"files_changed": 1, "additions": 2, "deletions": 1}
        finding = body["findings"][0]
        assert finding["agent_type"] == "security"
        assert finding["file_path"] == "app/db.py"
        assert finding["line_start"] == 42
    finally:
        await pool.execute("DELETE FROM finding_records WHERE review_id = $1", review_id)
        await pool.execute("DELETE FROM pr_review_records WHERE id = $1", review_id)


async def test_review_detail_404_when_not_found(client):
    resp = await client.get(f"/api/reviews/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_review_detail_diff_stats_none_without_github_client(pool):
    """No `github` wired into create_api_app at all -> diff_stats is None,
    not a crash (this is the app.py default `create_api_app(pool)` shape
    M7's test_lists_review_with_summed_cost also exercises)."""
    review_id = str(uuid.uuid4())
    delivery_id = f"m16b-{uuid.uuid4().hex[:12]}"
    await pool.execute(
        """
        INSERT INTO pr_review_records (id, repo, pr_number, github_delivery_id, status)
        VALUES ($1, 'test-fixture/m16b', 1, $2, 'posted')
        """,
        review_id,
        delivery_id,
    )
    try:
        app = create_api_app(pool)
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {os.environ['API_AUTH_TOKEN']}"}
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test", headers=headers) as c:
            resp = await c.get(f"/api/reviews/{review_id}")
        assert resp.status_code == 200
        assert resp.json()["diff_stats"] is None
    finally:
        await pool.execute("DELETE FROM pr_review_records WHERE id = $1", review_id)
