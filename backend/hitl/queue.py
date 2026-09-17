"""The human approval queue — one row in `hitl_reviews` per review the
confidence-weighted gate (L7) declined to auto-post.

Two reasons land a review here:
  - overall confidence below the gate threshold  → status 'pending'
  - any CRITICAL finding (regardless of confidence) → status 'pending', reason names it

Both are the same table; the `reason` text distinguishes them and the M7
dashboard / a human reviewer reads it.

M15: resolution. `resolve()` is the idempotency guard for approve/reject — a
single conditional UPDATE (`WHERE status = 'pending'`) that either claims the
row (returns True, and it's now safe to act on — e.g. post to GitHub) or finds
it already resolved (returns False, `0 rows affected`, so the caller can
answer 409 without a second read-then-write race).

Shares the asyncpg pool with `integrations.truth_store` — same Tiger Cloud
store (`single_data_spine`), injected rather than opened here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no py.typed marker


@dataclass(frozen=True)
class PendingHitlItem:
    id: str
    review_id: str
    repo: str
    pr_number: int
    reason: str
    created_at: datetime


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

    async def list_pending(self) -> list[PendingHitlItem]:
        rows = await self._pool.fetch(
            """
            SELECT hr.id, hr.review_id, pr.repo, pr.pr_number, hr.reason, hr.created_at
            FROM hitl_reviews hr
            JOIN pr_review_records pr ON pr.id = hr.review_id
            WHERE hr.status = 'pending'
            ORDER BY hr.created_at
            """
        )
        return [
            PendingHitlItem(
                id=str(row["id"]),
                review_id=str(row["review_id"]),
                repo=row["repo"],
                pr_number=row["pr_number"],
                reason=row["reason"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def resolve(self, *, review_id: str, status: str, note: str | None) -> bool:
        """Claim the pending hitl_reviews row for `review_id` by transitioning
        it to `status` ('resolved' or 'dismissed'). Returns True iff a pending
        row was actually claimed by this call — False means it was already
        resolved (by a prior call, or a concurrent one), and the caller must
        not perform the associated side effect (e.g. posting to GitHub)."""
        row = await self._pool.fetchrow(
            """
            UPDATE hitl_reviews
            SET status = $2, resolved_at = now(), resolution_note = $3
            WHERE review_id = $1 AND status = 'pending'
            RETURNING id
            """,
            review_id,
            status,
            note,
        )
        return row is not None
