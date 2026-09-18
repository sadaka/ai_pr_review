"""The read API process for the status frontend (M7) + the HITL action API (M15).

Deliberately separate from `webhook_receiver/app.py` — ingress and the read
API have different scaling and failure profiles (ADR-002 keeps them as distinct
mountable units in one repo). `create_api_app` takes injected collaborators so
tests drive it in-process without a live server or real GitHub App; `build_default_app`
opens them on startup via the lifespan hook.

`dependency_direction`: `backend/api` has no declared edge to `backend/integrations`
in `context-graph.json` — `api/hitl.py` itself only knows narrow Protocols. This
module (`app.py`) is the composition root that imports the concrete
`HitlQueue`/`TruthStore`/`GitHubAppClient` and wires them in, the same accepted
pattern `job_queue/arq_worker.py::startup()` uses (see M11's checkpoint). A
context-graph refresh to declare these edges explicitly is still carried-forward
housekeeping, not new debt introduced here.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import asyncpg  # type: ignore[import-untyped]
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.hitl import SupportsHitlQueue, SupportsPostReview, SupportsTruthStore, create_hitl_router
from api.repos import create_repos_router
from api.reviews import SupportsGetDiff, create_reviews_router
from auth.token_auth import require_auth
from hitl.queue import HitlQueue
from integrations.github_client import GitHubAppClient
from integrations.truth_store import TruthStore

_COMMAND_TIMEOUT_SECONDS = 10.0  # outbound_call_safety


def _cors_origins() -> list[str]:
    """The frontend runs on its own origin (e.g. localhost:3000) and calls this
    API cross-origin with an `Authorization` header, which browsers preflight
    via `OPTIONS` — without CORS middleware that preflight 405s before the
    real request is ever sent. Comma-separated `CORS_ALLOWED_ORIGINS` overrides
    the local-dev default."""
    raw = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _add_cors(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,  # auth is a bearer header, not cookies
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


class SupportsGitHub(SupportsPostReview, SupportsGetDiff, Protocol):
    """What this module needs from a GitHub client — both the M5 posting path
    (via api/hitl.py) and the M16 diff-stat lookup (via api/reviews.py)."""


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        database_url, command_timeout=_COMMAND_TIMEOUT_SECONDS, min_size=1, max_size=5
    )


def create_api_app(
    pool: asyncpg.Pool,
    *,
    hitl: SupportsHitlQueue | None = None,
    truth_store: SupportsTruthStore | None = None,
    github: SupportsGitHub | None = None,
) -> FastAPI:
    """For tests / callers that already own a pool (and, for the HITL router,
    optionally a fake `github`/`truth_store`/`hitl` instead of the real ones)."""
    hitl_queue: SupportsHitlQueue = hitl or HitlQueue(pool)
    truth_store_impl: SupportsTruthStore = truth_store or TruthStore(pool)
    github_client = github

    def _get_github() -> SupportsPostReview:
        if github_client is None:
            raise RuntimeError("no GitHub client configured for this app instance")
        return github_client

    app = FastAPI(title="ai-pr-review read API")
    _add_cors(app)
    app.include_router(
        create_reviews_router(lambda: pool, get_github=lambda: github_client),
        dependencies=[Depends(require_auth)],
    )
    app.include_router(create_repos_router(lambda: pool), dependencies=[Depends(require_auth)])
    app.include_router(
        create_hitl_router(lambda: hitl_queue, lambda: truth_store_impl, _get_github),
        dependencies=[Depends(require_auth)],
    )
    return app


def build_default_app() -> FastAPI:  # pragma: no cover - wired at real runtime, not under test
    """Production entrypoint: `uvicorn api.app:build_default_app --factory`.
    Opens the pool + GitHub App client on startup so they live on the server's
    event loop."""
    database_url = os.environ["TIGER_DATABASE_URL"]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pool = await create_pool(database_url)
        app.state.github = GitHubAppClient.from_env(dict(os.environ))
        try:
            yield
        finally:
            await app.state.github.aclose()
            await app.state.pool.close()

    app = FastAPI(title="ai-pr-review read API", lifespan=lifespan)
    _add_cors(app)
    app.include_router(
        create_reviews_router(lambda: app.state.pool, get_github=lambda: app.state.github),
        dependencies=[Depends(require_auth)],
    )
    app.include_router(
        create_repos_router(lambda: app.state.pool), dependencies=[Depends(require_auth)]
    )
    app.include_router(
        create_hitl_router(
            lambda: HitlQueue(app.state.pool),
            lambda: TruthStore(app.state.pool),
            lambda: app.state.github,
        ),
        dependencies=[Depends(require_auth)],
    )
    return app
