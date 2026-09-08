"""Hybrid retrieval over `code_chunks` (memory lane of the M1 Tiger Cloud data
spine): DiskANN vector similarity + Postgres full-text search, combined by
reciprocal rank fusion (RRF). This is the "grounding" that separates this
project from a single-LLM diff-only reviewer (ADR — see
`ai-pr-review-agent.html` L4): specialist agents reason over retrieved
codebase context, never the diff in isolation.

`single_data_spine` invariant: this is the only module that reads
`code_chunks` — everything else in `agents/**` goes through `ContextRetriever`.
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no py.typed marker
from openai import AsyncOpenAI
from pgvector.asyncpg import register_vector

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

EMBEDDING_MODEL = "text-embedding-3-small"
# Must match code_chunks.embedding's column type (vector(256)) from the M1
# migration — OpenAI's embeddings API truncates via Matryoshka representation
# when `dimensions` is passed, rather than requiring a different model.
EMBEDDING_DIMENSIONS = 256
RRF_K = 60  # standard reciprocal-rank-fusion damping constant
_MAX_QUERY_CHARS = 8000  # cap what we send to the embeddings API per query


@dataclass(frozen=True)
class RetrievedChunk:
    path: str
    content: str
    score: float


async def create_pool(database_url: str) -> asyncpg.Pool:
    """Create a connection pool with the pgvector codec registered on every
    connection (required so `embedding <=> $1` can bind a Python list
    directly) and an explicit command timeout (`outbound_call_safety`)."""

    async def _init(conn: asyncpg.Connection) -> None:
        await register_vector(conn)

    return await asyncpg.create_pool(
        database_url,
        init=_init,
        command_timeout=DB_COMMAND_TIMEOUT_SECONDS,
        min_size=1,
        max_size=5,
    )


class ContextRetriever:
    def __init__(
        self,
        pool: asyncpg.Pool,
        embeddings_client: AsyncOpenAI,
        *,
        embedding_model: str = EMBEDDING_MODEL,
    ) -> None:
        self._pool = pool
        self._embeddings = embeddings_client
        self._embedding_model = embedding_model
        self._db_guard = Guard(
            breaker=CircuitBreaker(name="tiger-retrieval"),
            timeout_seconds=DB_COMMAND_TIMEOUT_SECONDS,
            retry_on=_RETRYABLE_DB,
        )
        # attempts=1: the OpenAI SDK client already retries internally; this
        # layer adds the breaker + an outer timeout.
        self._embed_guard = Guard(
            breaker=CircuitBreaker(name="openai-embeddings"),
            attempts=1,
            timeout_seconds=LLM_TIMEOUT_SECONDS,
        )

    async def embed(self, text: str) -> list[float]:
        async def _call() -> list[float]:
            response = await self._embeddings.embeddings.create(
                model=self._embedding_model,
                input=text[:_MAX_QUERY_CHARS] or " ",
                dimensions=EMBEDDING_DIMENSIONS,
            )
            return response.data[0].embedding

        return await self._embed_guard(_call, name="context_retriever.embed")

    async def retrieve(self, *, repo: str, query_text: str, k: int = 5) -> list[RetrievedChunk]:
        """Hybrid retrieval, scoped to one repo. Returns up to `k` chunks
        ranked by RRF over (vector-similarity rank, full-text-search rank)."""
        query_embedding = await self.embed(query_text)

        async def _fetch() -> list[asyncpg.Record]:
            async with self._pool.acquire() as conn:
                return await conn.fetch(
                    _HYBRID_QUERY,
                    query_embedding,
                    repo,
                    k,
                    query_text[:2000] or " ",
                    RRF_K,
                )

        rows = await self._db_guard(_fetch, name="context_retriever.retrieve")
        return [RetrievedChunk(path=r["path"], content=r["content"], score=r["rrf_score"]) for r in rows]


_HYBRID_QUERY = """
                WITH vector_ranked AS (
                    SELECT id, path, content, row_number() OVER (ORDER BY embedding <=> $1) AS rank
                    FROM code_chunks
                    WHERE repo = $2
                    ORDER BY embedding <=> $1
                    LIMIT $3
                ),
                fts_ranked AS (
                    SELECT id, path, content,
                           row_number() OVER (ORDER BY ts_rank_cd(content_tsv, plainto_tsquery('english', $4)) DESC) AS rank
                    FROM code_chunks
                    WHERE repo = $2 AND content_tsv @@ plainto_tsquery('english', $4)
                    ORDER BY ts_rank_cd(content_tsv, plainto_tsquery('english', $4)) DESC
                    LIMIT $3
                )
                SELECT
                    COALESCE(v.id, f.id) AS id,
                    COALESCE(v.path, f.path) AS path,
                    COALESCE(v.content, f.content) AS content,
                    (1.0 / ($5 + COALESCE(v.rank, 1000000)))
                        + (1.0 / ($5 + COALESCE(f.rank, 1000000))) AS rrf_score
                FROM vector_ranked v
                FULL OUTER JOIN fts_ranked f ON f.id = v.id
                ORDER BY rrf_score DESC
                LIMIT $3
"""
