"""GET /api/reviews — the read model behind the status frontend (M7).
GET /api/reviews/{id} — one review's findings + diff stats, added in M16 for
the dashboard's review-detail page.

One row per review from `pr_review_records`, with its total spend summed from
the `agent_events` spine (M6) in the same query — no N+1. Read-only; the
frontend never reaches past this into the database (`frontend -> backend/api`
edge, ADR-002).

Diff stats (files changed / additions / deletions) aren't persisted anywhere
(no milestone stores them) — computed on demand from the same unified diff
`get_pull_request_diff` already fetches for the specialists (M11), reusing the
existing GitHub App auth path, no new credential. Best-effort: if no GitHub
client is wired in (e.g. a test using `create_api_app` without one) or the
fetch fails (PR closed/deleted since review time), `diff_stats` is `None`
rather than failing the whole detail view — a stat that only sometimes exists
is more honest than one silently reading zero.

`single_data_spine`: reads the one Tiger store. `outbound_call_safety`: the
pool carries a `command_timeout`; this is an internal read-only endpoint with
no third-party outbound for the DB reads, and the diff fetch reuses
`github_client`'s own breaker/retry/timeout wrapping.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import asyncpg  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_REVIEW_LIMIT = 100


class SupportsGetDiff(Protocol):
    async def get_pull_request_diff(self, *, repo_full_name: str, pr_number: int) -> str: ...


class ReviewSummary(BaseModel):
    id: str
    repo: str
    pr_number: int
    status: str
    confidence: float | None
    cost_usd: float
    created_at: datetime


class FindingSummary(BaseModel):
    agent_type: str
    severity: str
    category: str
    summary: str
    file_path: str
    line_start: int | None
    confidence: float
    rationale: str


class DiffStats(BaseModel):
    files_changed: int
    additions: int
    deletions: int


class ReviewDetail(ReviewSummary):
    findings: list[FindingSummary]
    diff_stats: DiffStats | None = None


def parse_diff_stats(diff_text: str) -> DiffStats:
    """Unified-diff line counting — `diff --git` marks a file boundary; a `+`/`-`
    line outside the `+++`/`---` file-header pair is an added/removed line."""
    files_changed = additions = deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            files_changed += 1
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return DiffStats(files_changed=files_changed, additions=additions, deletions=deletions)


_FINDINGS_QUERY = """
    SELECT agent_type, severity, category, summary, file_path, line_start, confidence, rationale
    FROM finding_records
    WHERE review_id = $1
    ORDER BY severity, file_path, line_start
"""


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


def create_reviews_router(
    get_pool: Callable[[], asyncpg.Pool],
    get_github: Callable[[], SupportsGetDiff | None] | None = None,
) -> APIRouter:
    """`get_pool` is resolved per request, so the pool can be created at app
    startup (lifespan) rather than at router-construction time. `get_github`
    is optional — omit it (as the plain M7 reviews-list use case does) and
    `diff_stats` on the detail endpoint is always `None`."""
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

    @router.get("/api/reviews/{review_id}", response_model=ReviewDetail)
    async def get_review(review_id: str) -> ReviewDetail:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT r.id, r.repo, r.pr_number, r.status,
                   r.overall_confidence AS confidence,
                   COALESCE(c.cost, 0)::float8 AS cost_usd,
                   r.created_at
            FROM pr_review_records r
            LEFT JOIN LATERAL (
                SELECT SUM(e.cost_usd) AS cost FROM agent_events e WHERE e.review_id = r.id
            ) c ON TRUE
            WHERE r.id = $1
            """,
            review_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="review not found")

        finding_rows = await pool.fetch(_FINDINGS_QUERY, review_id)

        diff_stats: DiffStats | None = None
        github = get_github() if get_github is not None else None
        if github is not None:
            try:
                diff_text = await github.get_pull_request_diff(
                    repo_full_name=row["repo"], pr_number=row["pr_number"]
                )
                diff_stats = parse_diff_stats(diff_text)
            except Exception:
                diff_stats = None

        return ReviewDetail(
            id=str(row["id"]),
            repo=row["repo"],
            pr_number=row["pr_number"],
            status=row["status"],
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            cost_usd=float(row["cost_usd"]),
            created_at=row["created_at"],
            findings=[
                FindingSummary(
                    agent_type=f["agent_type"],
                    severity=f["severity"],
                    category=f["category"],
                    summary=f["summary"],
                    file_path=f["file_path"],
                    line_start=f["line_start"],
                    confidence=float(f["confidence"]),
                    rationale=f["rationale"],
                )
                for f in finding_rows
            ],
            diff_stats=diff_stats,
        )

    return router
