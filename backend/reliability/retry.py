"""Retry-with-exponential-backoff for a single async operation.

Only exceptions in `retry_on` are retried; anything else propagates on the
first raise (a 400 from an API, a programming error — retrying those just wastes
time). After `attempts` tries the last exception is re-raised unchanged.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.1
DEFAULT_MAX_DELAY = 5.0


async def retry_async(
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await op()
        except retry_on as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(min(base_delay * (2**attempt), max_delay))
    assert last_exc is not None
    raise last_exc
