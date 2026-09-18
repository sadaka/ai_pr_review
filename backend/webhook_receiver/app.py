"""FastAPI ingress for GitHub webhooks: `pull_request` (M2, review work),
plus `installation` / `installation_repositories` / `push` (M13, ingestion).

Flow: verify HMAC signature -> dedup on X-GitHub-Delivery (applies to every
event type, not just `pull_request`) -> parse + enqueue the event-specific job
-> return 200 BEFORE any review/ingestion work happens.

For the three ingestion events, enqueuing also marks the repo `pending` in
`repo_index_state` (via `ingestion.repo_status.RepoStatusStore`) so the
dashboard can show "indexing" as soon as the webhook lands, not just once the
ARQ worker eventually finishes it.

Failure handling: any Redis failure (dedup claim OR enqueue) results in a 503,
never a silent 200. If the dedup claim succeeded but the enqueue then failed,
the claim is rolled back (`unclaim`) so a GitHub redelivery of the same
X-GitHub-Delivery id gets a genuine retry instead of being dropped as "already
processed."
"""
from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, Protocol

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import ValidationError

from api.schemas import (
    IndexRepoJob,
    InstallationPayload,
    InstallationRepositoriesPayload,
    PullRequestWebhookPayload,
    PushWebhookPayload,
    ReindexRepoJob,
    ReviewJob,
)
from webhook_receiver.signature import verify_signature

logger = logging.getLogger("webhook_receiver")


class SupportsJobQueue(Protocol):
    """Structural type for what the handler needs — real JobQueue and test
    doubles both satisfy this without a hard import-time dependency."""

    async def mark_seen(self, delivery_id: str) -> bool: ...
    async def unclaim(self, delivery_id: str) -> None: ...
    async def enqueue_review(self, job: ReviewJob) -> str | None: ...
    async def enqueue_index_repo(self, job: IndexRepoJob) -> str | None: ...
    async def enqueue_reindex_repo(self, job: ReindexRepoJob) -> str | None: ...


class SupportsRepoStatusStore(Protocol):
    """Structural type for `ingestion.repo_status.RepoStatusStore` — real store
    and test doubles both satisfy this without a hard import-time dependency."""

    async def mark_pending(self, repo_full_name: str) -> None: ...


class _NoOpRepoStatusStore:
    """Default when the caller (tests, mostly) doesn't care about status
    tracking — keeps `create_app`'s existing two-arg call sites working."""

    async def mark_pending(self, repo_full_name: str) -> None:
        return None


EnqueueOp = Callable[[SupportsJobQueue, SupportsRepoStatusStore], Awaitable[object]]


def _index_op(delivery_id: str, repo_full_name: str) -> EnqueueOp:
    async def _op(q: SupportsJobQueue, status_store: SupportsRepoStatusStore) -> object:
        await status_store.mark_pending(repo_full_name)
        return await q.enqueue_index_repo(IndexRepoJob(delivery_id=delivery_id, repo_full_name=repo_full_name))

    return _op


def _build_enqueue_ops(event: str, delivery_id: str, raw_body: bytes) -> list[EnqueueOp] | None:
    """Parse `raw_body` for `event` and return the list of enqueue operations
    to run once the dedup claim succeeds. `None` means "nothing to enqueue"
    (e.g. an `installation_repositories` event with no repos added) — the
    caller acks 200 without ever claiming the delivery id."""

    if event == "pull_request":
        pr_payload = PullRequestWebhookPayload.model_validate_json(raw_body)
        review_job = ReviewJob(
            delivery_id=delivery_id,
            repo_full_name=pr_payload.repository.full_name,
            pr_number=pr_payload.number,
            action=pr_payload.action,
        )

        async def _review_op(q: SupportsJobQueue, status_store: SupportsRepoStatusStore) -> object:
            return await q.enqueue_review(review_job)

        return [_review_op]

    if event == "installation":
        installation_payload = InstallationPayload.model_validate_json(raw_body)
        if installation_payload.action != "created" or not installation_payload.repositories:
            return None
        return [_index_op(delivery_id, repo.full_name) for repo in installation_payload.repositories]

    if event == "installation_repositories":
        repos_payload = InstallationRepositoriesPayload.model_validate_json(raw_body)
        if not repos_payload.repositories_added:
            return None
        return [_index_op(delivery_id, repo.full_name) for repo in repos_payload.repositories_added]

    if event == "push":
        push_payload = PushWebhookPayload.model_validate_json(raw_body)
        added: list[str] = []
        modified: list[str] = []
        removed: list[str] = []
        for commit in push_payload.commits:
            added.extend(commit.added)
            modified.extend(commit.modified)
            removed.extend(commit.removed)
        if not (added or modified or removed):
            return None
        reindex_job = ReindexRepoJob(
            delivery_id=delivery_id,
            repo_full_name=push_payload.repository.full_name,
            before_sha=push_payload.before,
            after_sha=push_payload.after,
            added=added,
            modified=modified,
            removed=removed,
        )

        async def _reindex_op(q: SupportsJobQueue, status_store: SupportsRepoStatusStore) -> object:
            await status_store.mark_pending(push_payload.repository.full_name)
            return await q.enqueue_reindex_repo(reindex_job)

        return [_reindex_op]

    return None  # pragma: no cover - unreachable, event already filtered by caller


def create_app(
    job_queue: SupportsJobQueue,
    webhook_secret: Callable[[], str] | str,
    repo_status_store: SupportsRepoStatusStore | None = None,
) -> FastAPI:
    """Build the FastAPI app. `job_queue` and `webhook_secret` are injected so
    tests can pass a fake queue / fixed secret instead of touching real Redis
    or environment state. `repo_status_store` defaults to a no-op so existing
    callers that don't care about status tracking are unaffected."""

    secret_getter = webhook_secret if callable(webhook_secret) else (lambda: webhook_secret)
    status_store = repo_status_store or _NoOpRepoStatusStore()
    app = FastAPI(title="ai-pr-review webhook ingress")

    @app.post("/webhook")
    async def receive_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
    ) -> Response:
        raw_body = await request.body()

        if not verify_signature(raw_body, secret_getter(), x_hub_signature_256):
            raise HTTPException(status_code=401, detail="invalid signature")

        if not x_github_delivery:
            raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery header")

        # Events with no ingestion/review work: ack cheaply, never enqueue.
        if x_github_event not in ("pull_request", "installation", "installation_repositories", "push"):
            return Response(status_code=200, content=f"ignored: unhandled event {x_github_event}")

        try:
            enqueue_ops = _build_enqueue_ops(x_github_event, x_github_delivery, raw_body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if enqueue_ops is None:  # a recognized event with nothing to do (e.g. no repos added)
            return Response(status_code=200, content=f"ignored: nothing to enqueue for {x_github_event}")

        try:
            is_new = await job_queue.mark_seen(x_github_delivery)
        except Exception:
            logger.exception("dedup claim failed for delivery %s", x_github_delivery)
            raise HTTPException(status_code=503, detail="job queue temporarily unavailable")

        if not is_new:
            logger.info("duplicate delivery %s — skipping enqueue", x_github_delivery)
            return Response(status_code=200, content="duplicate delivery, already processed")

        try:
            for op in enqueue_ops:
                await op(job_queue, status_store)
        except Exception:
            logger.exception("enqueue failed for delivery %s — rolling back dedup claim", x_github_delivery)
            try:
                await job_queue.unclaim(x_github_delivery)
            except Exception:
                # Rollback itself failed: the claim will still expire via TTL, so a
                # redelivery within 24h is lost but not longer than that. Logged loudly.
                logger.exception("rollback of dedup claim also failed for delivery %s", x_github_delivery)
            raise HTTPException(status_code=503, detail="job queue temporarily unavailable")

        return Response(status_code=200, content="accepted")

    return app


def build_default_app() -> FastAPI:  # pragma: no cover - wired up at real runtime, not under test
    """Production entrypoint: reads env vars directly. Tests use `create_app` instead.

    The Redis connection is opened in a FastAPI lifespan handler, not here — this
    function runs inside uvicorn's event loop when launched with `--factory`, so
    `asyncio.run()` at this point would raise "cannot be called from a running
    event loop". A small proxy forwards handler calls to the queue once connected.
    """
    from contextlib import asynccontextmanager

    import asyncpg  # type: ignore[import-untyped]

    from ingestion.repo_status import RepoStatusStore
    from job_queue.queue import JobQueue

    redis_url = os.environ["REDIS_URL"]
    webhook_secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    database_url = os.environ["TIGER_DATABASE_URL"]

    connected: dict[str, JobQueue] = {}
    pools: dict[str, asyncpg.Pool] = {}

    class _DeferredQueue:
        def _q(self) -> JobQueue:
            try:
                return connected["queue"]
            except KeyError:
                raise HTTPException(status_code=503, detail="job queue not connected yet")

        async def mark_seen(self, delivery_id: str) -> bool:
            return await self._q().mark_seen(delivery_id)

        async def unclaim(self, delivery_id: str) -> None:
            await self._q().unclaim(delivery_id)

        async def enqueue_review(self, job: ReviewJob) -> str | None:
            return await self._q().enqueue_review(job)

        async def enqueue_index_repo(self, job: IndexRepoJob) -> str | None:
            return await self._q().enqueue_index_repo(job)

        async def enqueue_reindex_repo(self, job: ReindexRepoJob) -> str | None:
            return await self._q().enqueue_reindex_repo(job)

    class _DeferredRepoStatusStore:
        def _store(self) -> RepoStatusStore:
            try:
                return RepoStatusStore(pools["db"])
            except KeyError:
                raise HTTPException(status_code=503, detail="database not connected yet")

        async def mark_pending(self, repo_full_name: str) -> None:
            await self._store().mark_pending(repo_full_name)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        connected["queue"] = await JobQueue.connect(redis_url)
        pools["db"] = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
        logger.info("job queue and database connected")
        try:
            yield
        finally:
            await connected["queue"].close()
            await pools["db"].close()

    app = create_app(_DeferredQueue(), webhook_secret, _DeferredRepoStatusStore())
    app.router.lifespan_context = lifespan
    return app
