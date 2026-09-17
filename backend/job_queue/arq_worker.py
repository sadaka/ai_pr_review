"""The ARQ worker that consumes the `run_review` jobs `webhook_receiver`
enqueues — the missing half of the pipeline until M11.

`run_review` fetches the PR diff, creates (or finds) the review row, and drives
the LangGraph review engine: fan out to the four M4 specialist agents, join at
`orchestrator.nodes.aggregate` (dedup → confidence gate → GitHub post / HITL
queue → truth-lane write).

This module is the pipeline's **composition root** — like
`webhook_receiver.build_default_app` and `api.app.build_default_app`, it is the
one place allowed to wire concrete objects across many modules (`integrations`,
`agents`, `memory`, `observability`, `orchestrator`). Following that
convention, those imports live inside `startup()`, not at module scope.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from arq.connections import RedisSettings

from api.schemas import IndexRepoJob, ReindexRepoJob, ReviewJob

if TYPE_CHECKING:
    from ingestion.indexer import Indexer
    from integrations.github_client import GitHubAppClient
    from integrations.truth_store import TruthStore
    from orchestrator.langgraph_engine import LangGraphEngine

_PRIOR_STATUS_DECISION = {
    "posted": "auto_post",
    "needs_human": "queued",
    "escalated": "escalated",
}


async def startup(ctx: dict[str, Any]) -> None:
    from redis.asyncio import Redis

    from agents.llm_client import create_llm_client
    from agents.specialists import DocsAgent, QualityAgent, SecurityAgent, TestsAgent
    from hitl.queue import HitlQueue
    from ingestion.indexer import Indexer
    from integrations.github_client import GitHubAppClient
    from integrations.truth_store import TruthStore
    from memory.context_retriever import ContextRetriever, create_pool
    from observability.events import AgentEventSink
    from orchestrator.graph import GraphDeps
    from orchestrator.langgraph_engine import LangGraphEngine

    pool = await create_pool(os.environ["TIGER_DATABASE_URL"])  # pgvector codec registered
    llm = create_llm_client(os.environ["OPENAI_API_KEY"])
    retriever = ContextRetriever(pool, llm)
    events = AgentEventSink(pool)

    agents = {
        "security": SecurityAgent(llm, retriever, events=events),
        "quality": QualityAgent(llm, retriever, events=events),
        "tests": TestsAgent(llm, retriever, events=events),
        "docs": DocsAgent(llm, retriever, events=events),
    }
    github = GitHubAppClient.from_env(dict(os.environ))
    truth_store = TruthStore(pool)
    hitl = HitlQueue(pool)
    indexer = Indexer(pool, llm, github)

    redis = Redis.from_url(os.environ["REDIS_URL"])
    engine = LangGraphEngine(
        redis,
        deps=GraphDeps(agents=agents, github=github, truth_store=truth_store, hitl=hitl, events=events),
    )

    ctx.update(pool=pool, redis=redis, github=github, truth_store=truth_store, engine=engine, indexer=indexer)


async def shutdown(ctx: dict[str, Any]) -> None:
    if github := ctx.get("github"):
        await github.aclose()
    if redis := ctx.get("redis"):
        await redis.aclose()
    if pool := ctx.get("pool"):
        await pool.close()


async def run_review(ctx: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    review_job = ReviewJob.model_validate(job)
    github: GitHubAppClient = ctx["github"]
    truth_store: TruthStore = ctx["truth_store"]
    engine: LangGraphEngine = ctx["engine"]

    review = await truth_store.upsert_review(
        repo=review_job.repo_full_name,
        pr_number=review_job.pr_number,
        delivery_id=review_job.delivery_id,
    )
    if review.status != "pending":
        # A redelivered webhook: the gate already ran once. Don't re-review.
        return {
            "review_id": review.id,
            "decision": _PRIOR_STATUS_DECISION.get(review.status, "queued"),
            "github_review_id": review.github_review_id,
            "replayed": True,
        }

    diff_text = await github.get_pull_request_diff(
        repo_full_name=review_job.repo_full_name, pr_number=review_job.pr_number
    )

    final_state = await engine.run(
        {
            "repo_full_name": review_job.repo_full_name,
            "pr_number": review_job.pr_number,
            "delivery_id": review_job.delivery_id,
            "diff_text": diff_text,
            "review_id": review.id,
            "specialist_results": [],
            "aggregated": None,
        },
        thread_id=f"review:{review_job.delivery_id}",
    )
    aggregated = final_state.get("aggregated")
    if aggregated is None:  # defensive — the aggregate node always populates this
        raise RuntimeError(f"review graph finished without an aggregate result: {review.id}")
    return aggregated


async def index_repo(ctx: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """`installation`/`installation_repositories` job: full-index a repo newly
    granted to the App."""
    index_job = IndexRepoJob.model_validate(job)
    indexer: Indexer = ctx["indexer"]
    result = await indexer.full_index(index_job.repo_full_name)
    return {
        "repo_full_name": result.repo_full_name,
        "commit_sha": result.commit_sha,
        "chunk_count": result.chunk_count,
    }


async def reindex_repo(ctx: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """`push` job: incrementally re-index only the files GitHub reports as
    added/modified, and delete chunks for removed files."""
    from ingestion.indexer import ChangedFiles

    reindex_job = ReindexRepoJob.model_validate(job)
    indexer: Indexer = ctx["indexer"]
    result = await indexer.incremental_index(
        reindex_job.repo_full_name,
        before_sha=reindex_job.before_sha,
        after_sha=reindex_job.after_sha,
        changed=ChangedFiles(
            added=reindex_job.added, modified=reindex_job.modified, removed=reindex_job.removed
        ),
    )
    return {
        "repo_full_name": result.repo_full_name,
        "commit_sha": result.commit_sha,
        "chunk_count": result.chunk_count,
    }


class WorkerSettings:
    """`arq job_queue.arq_worker.WorkerSettings` runs the worker. Tests drive
    `run_review` directly rather than via the arq CLI (see the M11 checkpoint's
    out-of-scope note), so this only needs to be importable + correct."""

    functions = [run_review, index_repo, reindex_repo]
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 3
    job_timeout = 300
    redis_settings = RedisSettings.from_dsn(os.environ.get("REDIS_URL", "redis://localhost:6379"))
