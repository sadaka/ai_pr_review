"""Marks `repo_index_state.status = 'pending'` the moment an indexing webhook
fires — before the job has even reached the ARQ worker, let alone started
running. This is the only writer of `pending`; the worker (`job_queue.arq_worker`)
flips it to `done`/`failed` once `ingestion.indexer.Indexer` finishes or errors.

Kept separate from `Indexer` because the webhook receiver has no reason to
depend on the rest of the indexing pipeline (chunker, embeddings, GitHub
client) just to record "we got the webhook."
"""
from __future__ import annotations

import asyncpg  # type: ignore[import-untyped]

_UPSERT_PENDING = """
    INSERT INTO repo_index_state (repo, status)
    VALUES ($1, 'pending')
    ON CONFLICT (repo) DO UPDATE SET status = 'pending'
"""


class RepoStatusStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def mark_pending(self, repo_full_name: str) -> None:
        await self._pool.execute(_UPSERT_PENDING, repo_full_name)
