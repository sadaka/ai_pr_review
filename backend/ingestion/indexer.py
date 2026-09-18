"""Writes chunked+embedded source into `code_chunks` — the production writer
the M1 migration provisioned the table for but that never got built.

`single_data_spine` invariant: `memory.context_retriever.ContextRetriever` is
the sole *reader* of `code_chunks`; this module is the sole *writer*. Neither
imports the other.

Two entry points:
- `full_index` — fetch the repo at HEAD, chunk+embed every eligible file,
  upsert everything, delete rows for paths no longer present, record
  `repo_index_state`. Driven by the `installation`/`installation_repositories`
  webhook events (a repo newly granted to the App).
- `incremental_index` — re-chunk/re-embed only the files a `push` event
  reports as added/modified, delete rows for removed files. Driven by `push`.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field

import asyncpg  # type: ignore[import-untyped]
from openai import AsyncOpenAI

from ingestion.chunker import Chunk, chunk_repo
from ingestion.source_fetcher import SupportsTarballFetch, fetch_tarball
from memory.context_retriever import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from reliability import (
    DB_COMMAND_TIMEOUT_SECONDS,
    LLM_TIMEOUT_SECONDS,
    CircuitBreaker,
    Guard,
    OperationTimeout,
)

_RETRYABLE_DB: tuple[type[BaseException], ...] = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    OperationTimeout,
    ConnectionError,
    OSError,
)
_MAX_EMBED_CHARS = 8000  # matches ContextRetriever's cap on the same embeddings API


@dataclass(frozen=True)
class ChangedFiles:
    """The files a `push` webhook reports as touched, aggregated across the
    push's commit list (GitHub-reported, not locally diffed — ADR-0005)."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def to_reembed(self) -> set[str]:
        return set(self.added) | set(self.modified)


@dataclass(frozen=True)
class IndexResult:
    repo_full_name: str
    commit_sha: str
    chunk_count: int
    files_embedded: int


class Indexer:
    def __init__(
        self,
        pool: asyncpg.Pool,
        embeddings_client: AsyncOpenAI,
        github: SupportsTarballFetch,
    ) -> None:
        self._pool = pool
        self._embeddings = embeddings_client
        self._github = github
        self._db_guard = Guard(
            breaker=CircuitBreaker(name="tiger-ingestion"),
            timeout_seconds=DB_COMMAND_TIMEOUT_SECONDS,
            retry_on=_RETRYABLE_DB,
        )
        # attempts=1: the OpenAI SDK already retries internally (same rationale
        # as ContextRetriever._embed_guard).
        self._embed_guard = Guard(
            breaker=CircuitBreaker(name="openai-ingestion-embeddings"),
            attempts=1,
            timeout_seconds=LLM_TIMEOUT_SECONDS,
        )

    async def embed(self, text: str) -> list[float]:
        async def _call() -> list[float]:
            response = await self._embeddings.embeddings.create(
                model=EMBEDDING_MODEL,
                input=text[:_MAX_EMBED_CHARS] or " ",
                dimensions=EMBEDDING_DIMENSIONS,
            )
            return response.data[0].embedding

        return await self._embed_guard(_call, name="ingestion.indexer.embed")

    async def _embed_chunks(self, chunks: list[Chunk]) -> list[tuple[Chunk, list[float]]]:
        return [(chunk, await self.embed(chunk.content)) for chunk in chunks]

    async def _write(
        self,
        *,
        repo_full_name: str,
        embedded: list[tuple[Chunk, list[float]]],
        delete_paths: set[str],
        commit_sha: str,
    ) -> None:
        async def _op() -> None:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    if delete_paths:
                        await conn.execute(
                            "DELETE FROM code_chunks WHERE repo = $1 AND path = ANY($2::text[])",
                            repo_full_name,
                            list(delete_paths),
                        )
                    touched_paths = {chunk.path for chunk, _ in embedded}
                    if touched_paths:
                        await conn.execute(
                            "DELETE FROM code_chunks WHERE repo = $1 AND path = ANY($2::text[])",
                            repo_full_name,
                            list(touched_paths),
                        )
                    if embedded:
                        await conn.executemany(
                            _UPSERT_CHUNK,
                            [
                                (
                                    repo_full_name,
                                    chunk.path,
                                    chunk.symbol,
                                    chunk.chunk_index,
                                    chunk.content,
                                    embedding,
                                    len(chunk.content.split()),
                                )
                                for chunk, embedding in embedded
                            ],
                        )
                    total_chunks = await conn.fetchval(
                        "SELECT count(*) FROM code_chunks WHERE repo = $1", repo_full_name
                    )
                    await conn.execute(
                        _UPSERT_REPO_INDEX_STATE,
                        repo_full_name,
                        commit_sha,
                        total_chunks,
                    )

        await self._db_guard(_op, name="ingestion.indexer.write")

    async def full_index(self, repo_full_name: str) -> IndexResult:
        fetched = await fetch_tarball(self._github, repo_full_name=repo_full_name)
        try:
            chunks = chunk_repo(fetched.root)
            embedded = await self._embed_chunks(chunks)
            retained_paths = {chunk.path for chunk in chunks}

            async def _existing_paths() -> list[str]:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT DISTINCT path FROM code_chunks WHERE repo = $1", repo_full_name
                    )
                    return [r["path"] for r in rows]

            existing_paths = set(await self._db_guard(_existing_paths, name="ingestion.indexer.existing_paths"))
            stale_paths = existing_paths - retained_paths

            await self._write(
                repo_full_name=repo_full_name,
                embedded=embedded,
                delete_paths=stale_paths,
                commit_sha=fetched.commit_sha,
            )
            return IndexResult(
                repo_full_name=repo_full_name,
                commit_sha=fetched.commit_sha,
                chunk_count=len(embedded),
                files_embedded=len(retained_paths),
            )
        finally:
            shutil.rmtree(fetched.root.parent, ignore_errors=True)

    async def incremental_index(
        self,
        repo_full_name: str,
        *,
        before_sha: str,
        after_sha: str,
        changed: ChangedFiles,
    ) -> IndexResult:
        reembed_paths = changed.to_reembed()
        chunks: list[Chunk] = []
        fetched = None
        if reembed_paths:
            fetched = await fetch_tarball(self._github, repo_full_name=repo_full_name, ref=after_sha)
            try:
                chunks = chunk_repo(fetched.root, only_paths=reembed_paths)
            finally:
                shutil.rmtree(fetched.root.parent, ignore_errors=True)

        embedded = await self._embed_chunks(chunks)
        await self._write(
            repo_full_name=repo_full_name,
            embedded=embedded,
            delete_paths=set(changed.removed),
            commit_sha=after_sha,
        )
        return IndexResult(
            repo_full_name=repo_full_name,
            commit_sha=after_sha,
            chunk_count=len(embedded),
            files_embedded=len(reembed_paths),
        )


_UPSERT_CHUNK = """
    INSERT INTO code_chunks (repo, path, symbol, chunk_index, content, embedding, token_count, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, now())
    ON CONFLICT (repo, path, chunk_index) DO UPDATE
        SET symbol = EXCLUDED.symbol,
            content = EXCLUDED.content,
            embedding = EXCLUDED.embedding,
            token_count = EXCLUDED.token_count,
            updated_at = now()
"""

_UPSERT_REPO_INDEX_STATE = """
    INSERT INTO repo_index_state (repo, last_indexed_commit, indexed_at, chunk_count)
    VALUES ($1, $2, now(), $3)
    ON CONFLICT (repo) DO UPDATE
        SET last_indexed_commit = EXCLUDED.last_indexed_commit,
            indexed_at = now(),
            chunk_count = EXCLUDED.chunk_count
"""
