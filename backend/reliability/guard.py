"""`Guard` — wrap an outbound operation once and get all three call-site
concerns in the right order:

    circuit breaker (fail fast if the dependency is known-down)
      → retry-with-backoff (ride out a transient blip)
        → timeout (don't hang forever on any single attempt)

A caller builds one `Guard` per downstream dependency (one breaker per
dependency) and reuses it for every call to that dependency.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from .circuit_breaker import CircuitBreaker
from .retry import retry_async
from .timeout import with_timeout

T = TypeVar("T")


class Guard:
    def __init__(
        self,
        *,
        breaker: CircuitBreaker | None = None,
        attempts: int = 3,
        base_delay: float = 0.1,
        timeout_seconds: float | None = None,
        retry_on: tuple[type[BaseException], ...] = (Exception,),
    ) -> None:
        self._breaker = breaker
        self._attempts = attempts
        self._base_delay = base_delay
        self._timeout_seconds = timeout_seconds
        self._retry_on = retry_on

    async def __call__(self, op: Callable[[], Awaitable[T]], *, name: str = "operation") -> T:
        if self._breaker is not None:
            self._breaker.before_call()

        async def _attempt() -> T:
            if self._timeout_seconds is None:
                return await op()
            return await with_timeout(op(), self._timeout_seconds, name=name)

        try:
            result = await retry_async(
                _attempt,
                attempts=self._attempts,
                base_delay=self._base_delay,
                retry_on=self._retry_on,
            )
        except asyncio.CancelledError:
            # A task cancellation is not a dependency failure — don't count it
            # toward opening the breaker.
            raise
        except BaseException:
            if self._breaker is not None:
                self._breaker.on_failure()
            raise
        if self._breaker is not None:
            self._breaker.on_success()
        return result
