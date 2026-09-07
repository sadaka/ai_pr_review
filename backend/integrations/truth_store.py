"""Truth-lane writes: `pr_review_records` + `finding_records`.

`single_data_spine` (ADR-003): this is durable truth, so it lives in the one
Tiger Cloud Postgres store — never Redis. `memory/context_retriever.py` owns
the `code_chunks` table; this module owns the review/finding tables. They share
the database, not code.

Idempotency: a review is keyed on `github_delivery_id` (UNIQUE index from the
M1 migration). `upsert_review` returns the existing row on a redelivery — with
its `github_review_id` populated if the review was already posted — so the
aggregator can skip re-posting instead of creating a duplicate GitHub review.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no py.typed marker

from agents.contracts import Finding

_COMMAND_TIMEOUT_SECONDS = 10.0  # outbound_call_safety


@dataclass(frozen=True)
class ReviewRow:
    id: str
    status: str
    github_review_id: int | None


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        database_url, command_timeout=_COMMAND_TIMEOUT_SECONDS, min_size=1, max_size=5
    )


class TruthStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> "TruthStore":
        return cls(await create_pool(database_url))

    async def close(self) -> None:
        await self._pool.close()

    async def upsert_review(self, *, repo: str, pr_number: int, delivery_id: str) -> ReviewRow:
        """Get-or-create the review row for this webhook delivery. The UPDATE on
        conflict is a deliberate no-op (re-sets pr_number to itself) so RETURNING
        always yields the row, new or existing."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO pr_review_records (repo, pr_number, github_delivery_id, status)
            VALUES ($1, $2, $3, 'pending')
            ON CONFLICT (github_delivery_id)
                DO UPDATE SET pr_number = EXCLUDED.pr_number
            RETURNING id, status, github_review_id
            """,
            repo,
            pr_number,
            delivery_id,
        )
        return ReviewRow(id=str(row["id"]), status=row["status"], github_review_id=row["github_review_id"])

    async def insert_findings(self, *, review_id: str, findings: Sequence[Finding]) -> None:
        if not findings:
            return
        await self._pool.executemany(
            """
            INSERT INTO finding_records
                (review_id, agent_type, severity, category, summary,
                 file_path, line_start, line_end, suggestion, confidence, rationale)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, $9, $10)
            """,
            [
                (
                    review_id,
                    f.agent_type.value,
                    f.severity.value,
                    f.category,
                    f.title,
                    f.file,
                    f.line,
                    f.line,
                    f.confidence,
                    f.rationale,
                )
                for f in findings
            ],
        )

    async def mark_posted(self, *, review_id: str, github_review_id: int, overall_confidence: float) -> None:
        await self._pool.execute(
            """
            UPDATE pr_review_records
            SET status = 'posted', github_review_id = $2, overall_confidence = $3, completed_at = now()
            WHERE id = $1
            """,
            review_id,
            github_review_id,
            overall_confidence,
        )

    async def set_status(self, *, review_id: str, status: str, overall_confidence: float) -> None:
        await self._pool.execute(
            """
            UPDATE pr_review_records
            SET status = $2, overall_confidence = $3, completed_at = now()
            WHERE id = $1
            """,
            review_id,
            status,
            overall_confidence,
        )
