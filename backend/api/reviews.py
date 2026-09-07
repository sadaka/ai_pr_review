"""GET /api/reviews — the read model behind the status frontend (M7).

One row per review from `pr_review_records`, with its total spend summed from
the `agent_events` spine (M6) in the same query — no N+1. Read-only; the
frontend never reaches past this into the database (`frontend -> backend/api`
edge, ADR-002).

`single_data_spine`: reads the one Tiger store. `outbound_call_safety`: the
pool carries a `command_timeout`; this is an internal read-only endpoint with
no third-party outbound, so no retry/breaker.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import asyncpg  # type: ignore[import-untyped]
from fastapi import APIRouter
from pydantic import BaseModel

_REVIEW_LIMIT = 100


class ReviewSummary(BaseModel):
    id: str
    repo: str
    pr_number: int
    status: str
    confidence: float | None
    cost_usd: float
    created_at: datetime


_QUERY = """
    SELECT r.id, r.repo, r.pr_number, r.status,
           r.overall_confidence AS confidence,
           COALESCE(c.cost, 0)::float8 AS cost_usd,
           r.created_at
    FROM pr_review_records r
    LEFT JOIN LATERAL (
        SELECT SUM(e.cost_usd) AS cost
        FROM agent_events e
        WHERE e.review_id = r.id
    ) c ON TRUE
    ORDER BY r.created_at DESC, r.id
    LIMIT $1
"""


def create_reviews_router(get_pool: Callable[[], asyncpg.Pool]) -> APIRouter:
    """`get_pool` is resolved per request, so the pool can be created at app
    startup (lifespan) rather than at router-construction time."""
    router = APIRouter()

    @router.get("/api/reviews", response_model=list[ReviewSummary])
    async def list_reviews() -> list[ReviewSummary]:
        rows = await get_pool().fetch(_QUERY, _REVIEW_LIMIT)
        return [
            ReviewSummary(
                id=str(row["id"]),
                repo=row["repo"],
                pr_number=row["pr_number"],
                status=row["status"],
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                cost_usd=float(row["cost_usd"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    return router
