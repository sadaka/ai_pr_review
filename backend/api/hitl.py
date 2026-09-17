"""GET/POST /api/hitl — resolve the human approval queue M5's `aggregate()`
writes to (M15). Behind M14's `require_auth`, wired by the caller.

`dependency_direction`: `backend/api` has no declared edge to
`backend/integrations` — only to `backend/hitl`/`backend/orchestrator`. This
module therefore names its own narrow Protocols (mirroring
`orchestrator/nodes.py`'s `SupportsPostReview`/`SupportsTruthStore`) instead of
importing `integrations.github_client`/`integrations.truth_store` directly.
The concrete `GitHubAppClient`/`TruthStore` are constructed and injected by
`api/app.py`'s `build_default_app` (a composition root, same accepted pattern
as `job_queue/arq_worker.py`'s `startup()` — see M11's checkpoint).

ADR-0008: approve reuses the exact same `github_client.post_review` +
`orchestrator.nodes.render_review_body` that M5's auto-post path uses — no
second, divergent posting implementation. `HitlQueue.resolve()` is the
idempotency guard (an atomic `WHERE status = 'pending'` UPDATE): a second
approve/reject call on an already-resolved row gets 409, never a second post.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from orchestrator.nodes import render_review_body

# Type-only: no runtime import, so this stays a static-analysis convenience,
# not a real `backend/api -> backend/integrations` dependency (undeclared in
# context-graph.json — see the module docstring). Using the concrete dataclasses
# here (rather than re-declaring parallel Protocol shapes) sidesteps mypy's
# nominal/invariance quirks around Optional/list-of-dataclass return types.
if TYPE_CHECKING:
    from hitl.queue import PendingHitlItem
    from integrations.truth_store import ReviewWithFindings


class SupportsPostReview(Protocol):
    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int: ...


class SupportsTruthStore(Protocol):
    async def get_review_with_findings(self, *, review_id: str) -> "ReviewWithFindings | None": ...
    async def mark_posted(self, *, review_id: str, github_review_id: int, overall_confidence: float) -> None: ...


class SupportsHitlQueue(Protocol):
    async def list_pending(self) -> list["PendingHitlItem"]: ...
    async def resolve(self, *, review_id: str, status: str, note: str | None) -> bool: ...


class PendingHitlItemResponse(BaseModel):
    id: str
    review_id: str
    repo: str
    pr_number: int
    reason: str
    created_at: datetime


class ResolveRequest(BaseModel):
    note: str | None = None


class ResolveResponse(BaseModel):
    review_id: str
    status: str
    github_review_id: int | None = None


def create_hitl_router(
    get_hitl: Callable[[], SupportsHitlQueue],
    get_truth_store: Callable[[], SupportsTruthStore],
    get_github: Callable[[], SupportsPostReview],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/hitl", response_model=list[PendingHitlItemResponse])
    async def list_pending() -> list[PendingHitlItemResponse]:
        items = await get_hitl().list_pending()
        return [
            PendingHitlItemResponse(
                id=item.id,
                review_id=item.review_id,
                repo=item.repo,
                pr_number=item.pr_number,
                reason=item.reason,
                created_at=item.created_at,
            )
            for item in items
        ]

    @router.post("/api/hitl/{review_id}/approve", response_model=ResolveResponse)
    async def approve(review_id: str, body: ResolveRequest | None = None) -> ResolveResponse:
        note = body.note if body is not None else None
        claimed = await get_hitl().resolve(review_id=review_id, status="resolved", note=note)
        if not claimed:
            raise HTTPException(status_code=409, detail="already resolved")

        truth_store = get_truth_store()
        review = await truth_store.get_review_with_findings(review_id=review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="review not found")

        event = "APPROVE" if not review.findings else "COMMENT"
        github_review_id = await get_github().post_review(
            repo_full_name=review.repo,
            pr_number=review.pr_number,
            body=render_review_body(review.findings),
            event=event,
        )
        await truth_store.mark_posted(
            review_id=review_id,
            github_review_id=github_review_id,
            overall_confidence=review.overall_confidence or 0.0,
        )
        return ResolveResponse(review_id=review_id, status="resolved", github_review_id=github_review_id)

    @router.post("/api/hitl/{review_id}/reject", response_model=ResolveResponse)
    async def reject(review_id: str, body: ResolveRequest | None = None) -> ResolveResponse:
        note = body.note if body is not None else None
        claimed = await get_hitl().resolve(review_id=review_id, status="dismissed", note=note)
        if not claimed:
            raise HTTPException(status_code=409, detail="already resolved")
        return ResolveResponse(review_id=review_id, status="dismissed", github_review_id=None)

    return router
