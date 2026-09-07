"""The human approval queue — one row in `hitl_reviews` per review the
confidence-weighted gate (L7) declined to auto-post.

Two reasons land a review here:
  - overall confidence below the gate threshold  → status 'pending'
  - any CRITICAL finding (regardless of confidence) → status 'pending', reason names it

Both are the same table; the `reason` text distinguishes them and the M7
dashboard / a human reviewer reads it. Resolution (`status`, `resolved_at`) is
out of M5 scope.

Shares the asyncpg pool with `integrations.truth_store` — same Tiger Cloud
store (`single_data_spine`), injected rather than opened here.
"""
from __future__ import annotations

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no py.typed marker


class HitlQueue:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def enqueue(self, *, review_id: str, reason: str) -> str:
        """Insert a pending human-review row for `review_id`. Returns the new hitl_reviews id."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO hitl_reviews (review_id, reason, status)
            VALUES ($1, $2, 'pending')
            RETURNING id
            """,
            review_id,
            reason,
        )
        return str(row["id"])

    async def pending_count(self, *, review_id: str) -> int:
        """Test/observability helper — how many pending rows exist for a review."""
        return await self._pool.fetchval(
            "SELECT count(*) FROM hitl_reviews WHERE review_id = $1 AND status = 'pending'",
            review_id,
        )
