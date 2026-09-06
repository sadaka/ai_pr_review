"""FastAPI ingress for GitHub `pull_request` webhooks.

Flow: verify HMAC signature -> parse minimal payload -> dedup on
X-GitHub-Delivery -> enqueue a review job -> return 200 BEFORE any review work
happens (the whole point of M2: ingress is cheap and fast, review work is
someone else's job, later).

Failure handling: any Redis failure (dedup claim OR enqueue) results in a 503,
never a silent 200. If the dedup claim succeeded but the enqueue then failed,
the claim is rolled back (`unclaim`) so a GitHub redelivery of the same
X-GitHub-Delivery id gets a genuine retry instead of being dropped as "already
processed."
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Protocol

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import ValidationError

from api.schemas import PullRequestWebhookPayload, ReviewJob
from webhook_receiver.signature import verify_signature

logger = logging.getLogger("webhook_receiver")


class SupportsJobQueue(Protocol):
    """Structural type for what the handler needs — real JobQueue and test
    doubles both satisfy this without a hard import-time dependency."""

    async def mark_seen(self, delivery_id: str) -> bool: ...
    async def unclaim(self, delivery_id: str) -> None: ...
    async def enqueue_review(self, job: ReviewJob) -> str | None: ...


def create_app(job_queue: SupportsJobQueue, webhook_secret: Callable[[], str] | str) -> FastAPI:
    """Build the FastAPI app. `job_queue` and `webhook_secret` are injected so
    tests can pass a fake queue / fixed secret instead of touching real Redis
    or environment state."""

    secret_getter = webhook_secret if callable(webhook_secret) else (lambda: webhook_secret)
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

        # Only pull_request events carry review work; ack everything else cheaply.
        if x_github_event != "pull_request":
            return Response(status_code=200, content="ignored: not a pull_request event")

        try:
            payload = PullRequestWebhookPayload.model_validate_json(raw_body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            is_new = await job_queue.mark_seen(x_github_delivery)
        except Exception:
            logger.exception("dedup claim failed for delivery %s", x_github_delivery)
            raise HTTPException(status_code=503, detail="job queue temporarily unavailable")

        if not is_new:
            logger.info("duplicate delivery %s — skipping enqueue", x_github_delivery)
            return Response(status_code=200, content="duplicate delivery, already processed")

        job = ReviewJob(
            delivery_id=x_github_delivery,
            repo_full_name=payload.repository.full_name,
            pr_number=payload.number,
            action=payload.action,
        )
        try:
            await job_queue.enqueue_review(job)
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
    """Production entrypoint: reads env vars directly. Tests use `create_app` instead."""
    import asyncio

    from job_queue.queue import JobQueue

    redis_url = os.environ["REDIS_URL"]
    webhook_secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    queue = asyncio.run(JobQueue.connect(redis_url))
    return create_app(queue, webhook_secret)
