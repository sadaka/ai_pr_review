"""GET /api/repos — the M16 dashboard's repo list: each repo M13's ingestion
pipeline has indexed, joined with how many reviews it's had.

`single_data_spine`: reads the one Tiger store (`repo_index_state` from M13 +
`pr_review_records`). Read-only, no third-party outbound.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import asyncpg  # type: ignore[import-untyped]
from fastapi import APIRouter
from pydantic import BaseModel


class RepoSummary(BaseModel):
    repo: str
    last_indexed_commit: str
    indexed_at: datetime
    chunk_count: int
    review_count: int


_QUERY = """
    SELECT ris.repo, ris.last_indexed_commit, ris.indexed_at, ris.chunk_count,
           COALESCE(rc.review_count, 0) AS review_count
    FROM repo_index_state ris
    LEFT JOIN LATERAL (
        SELECT count(*) AS review_count
        FROM pr_review_records pr
        WHERE pr.repo = ris.repo
    ) rc ON TRUE
    ORDER BY ris.indexed_at DESC
"""


def create_repos_router(get_pool: Callable[[], asyncpg.Pool]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/repos", response_model=list[RepoSummary])
    async def list_repos() -> list[RepoSummary]:
        rows = await get_pool().fetch(_QUERY)
        return [
            RepoSummary(
                repo=row["repo"],
                last_indexed_commit=row["last_indexed_commit"],
                indexed_at=row["indexed_at"],
                chunk_count=row["chunk_count"],
                review_count=row["review_count"],
            )
            for row in rows
        ]

    return router
