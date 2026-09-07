"""The read API process for the status frontend (M7).

Deliberately separate from `webhook_receiver/app.py` — ingress and the read
API have different scaling and failure profiles (ADR-002 keeps them as distinct
mountable units in one repo). `create_api_app` takes an injected asyncpg pool
so tests drive it in-process without a live server; `build_default_app` opens
the pool on startup via the lifespan hook.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg  # type: ignore[import-untyped]
from fastapi import FastAPI

from api.reviews import create_reviews_router

_COMMAND_TIMEOUT_SECONDS = 10.0  # outbound_call_safety


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        database_url, command_timeout=_COMMAND_TIMEOUT_SECONDS, min_size=1, max_size=5
    )


def create_api_app(pool: asyncpg.Pool) -> FastAPI:
    """For tests / callers that already own a pool."""
    app = FastAPI(title="ai-pr-review read API")
    app.include_router(create_reviews_router(lambda: pool))
    return app


def build_default_app() -> FastAPI:  # pragma: no cover - wired at real runtime, not under test
    """Production entrypoint: `uvicorn api.app:build_default_app --factory`.
    Opens the pool on startup so it lives on the server's event loop."""
    database_url = os.environ["TIGER_DATABASE_URL"]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pool = await create_pool(database_url)
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(title="ai-pr-review read API", lifespan=lifespan)
    app.include_router(create_reviews_router(lambda: app.state.pool))
    return app
