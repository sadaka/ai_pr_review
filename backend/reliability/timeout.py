"""Per-operation timeout, plus the one place the outbound timeout constants
live (they were previously scattered as module-private constants across
`llm_client`, `github_client`, `context_retriever`, `truth_store`, `queue`).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

T = TypeVar("T")

LLM_TIMEOUT_SECONDS = 30.0
GITHUB_TIMEOUT_SECONDS = 10.0
DB_COMMAND_TIMEOUT_SECONDS = 10.0
REDIS_TIMEOUT_SECONDS = 5.0


class OperationTimeout(RuntimeError):
    """An operation did not complete within its allotted time."""


async def with_timeout(coro: Awaitable[T], seconds: float, *, name: str = "operation") -> T:
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError as exc:
        raise OperationTimeout(f"{name} exceeded {seconds}s") from exc
