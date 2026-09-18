"""M16 demo (partial): pytest backend/tests/test_repos_api.py -k
"repos_list_includes_index_state"

Live truth lane, same ASGITransport pattern as test_reviews_api.py. Seeds one
repo_index_state row + a review for the same repo, asserts GET /api/repos
returns the index state fields plus the review count. Skips cleanly without
TIGER_DATABASE_URL.
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
    os.environ.setdefault("API_AUTH_TOKEN", "test-fixture-token")
    app = create_api_app(pool)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {os.environ['API_AUTH_TOKEN']}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test", headers=headers) as c:
        yield c


async def test_repos_list_includes_index_state(client, pool):
    repo = f"test-fixture/m16-{uuid.uuid4().hex[:8]}"
    delivery_id = f"m16-{uuid.uuid4().hex[:12]}"
    review_id = str(uuid.uuid4())

    await pool.execute(
        """
        INSERT INTO repo_index_state (repo, last_indexed_commit, chunk_count)
        VALUES ($1, 'deadbeef', 42)
        """,
        repo,
    )
    await pool.execute(
        """
        INSERT INTO pr_review_records (id, repo, pr_number, github_delivery_id, status)
        VALUES ($1, $2, 1, $3, 'posted')
        """,
        review_id,
        repo,
        delivery_id,
    )

    try:
        resp = await client.get("/api/repos")
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["repo"] == repo)
        assert row["last_indexed_commit"] == "deadbeef"
        assert row["chunk_count"] == 42
        assert row["review_count"] == 1
        assert row["status"] == "done"  # default backfilled by the status migration
        assert row["error"] is None
    finally:
        await pool.execute("DELETE FROM pr_review_records WHERE id = $1", review_id)
        await pool.execute("DELETE FROM repo_index_state WHERE repo = $1", repo)


async def test_repos_list_empty_when_no_rows(client):
    resp = await client.get("/api/repos")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_repos_list_shows_pending_repo_with_no_commit_yet(client, pool):
    """A repo whose webhook just fired (`ingestion.repo_status.RepoStatusStore`)
    has no successful index yet — no commit, no chunks — but should still show
    up as `pending` rather than being absent from the dashboard."""
    repo = f"test-fixture/m16-pending-{uuid.uuid4().hex[:8]}"

    await pool.execute(
        "INSERT INTO repo_index_state (repo, status) VALUES ($1, 'pending')", repo
    )

    try:
        resp = await client.get("/api/repos")
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["repo"] == repo)
        assert row["status"] == "pending"
        assert row["last_indexed_commit"] is None
        assert row["chunk_count"] == 0
    finally:
        await pool.execute("DELETE FROM repo_index_state WHERE repo = $1", repo)


async def test_repos_list_shows_failed_repo_with_error(client, pool):
    repo = f"test-fixture/m16-failed-{uuid.uuid4().hex[:8]}"

    await pool.execute(
        "INSERT INTO repo_index_state (repo, status, error) VALUES ($1, 'failed', $2)",
        repo,
        "tarball fetch failed: 404",
    )

    try:
        resp = await client.get("/api/repos")
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["repo"] == repo)
        assert row["status"] == "failed"
        assert row["error"] == "tarball fetch failed: 404"
    finally:
        await pool.execute("DELETE FROM repo_index_state WHERE repo = $1", repo)
