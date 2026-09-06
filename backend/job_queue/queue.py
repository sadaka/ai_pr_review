"""Redis-backed job queue: dedup + ARQ enqueue.

Redis here is queue/cache only (never durable truth — ADR-003 `single_data_spine`).

Outbound-call-safety (context-graph invariant): every Redis call goes through an
explicit connect timeout AND per-command socket timeout, retries transient
failures with exponential backoff, and trips a circuit breaker after repeated
failures so a dead Redis fails fast instead of hanging every webhook request.

Claim/rollback for the dedup key (not just "set and forget") is what makes a
failed enqueue-after-dedup safe: `mark_seen` claims the delivery id, and if the
subsequent enqueue never succeeds, `unclaim` releases it so a GitHub redelivery
gets a genuine retry instead of being silently swallowed as "already processed."
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from arq.connections import ArqRedis, RedisSettings

from api.schemas import ReviewJob

DELIVERY_DEDUP_TTL_SECONDS = 24 * 60 * 60  # GitHub redelivers for up to 24h
REDIS_CONNECT_TIMEOUT_SECONDS = 5
REDIS_COMMAND_TIMEOUT_SECONDS = 5

_FAILURE_THRESHOLD = 3
_RESET_AFTER_SECONDS = 30

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.1


class CircuitOpenError(RuntimeError):
    """Raised instead of attempting a Redis call while the breaker is open."""


class JobEnqueueError(RuntimeError):
    """Raised when enqueueing a job failed after all retries were exhausted."""


@dataclass
class CircuitBreaker:
    """Minimal fail-fast breaker: opens after N consecutive failures, half-opens after a cooldown."""

    failure_threshold: int = _FAILURE_THRESHOLD
    reset_after_seconds: float = _RESET_AFTER_SECONDS
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def before_call(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at >= self.reset_after_seconds:
            return  # half-open: let the next call through as a probe
        raise CircuitOpenError("job queue circuit breaker is open — redis unavailable")

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()


async def _with_retry(op, *, attempts: int = _RETRY_ATTEMPTS, base_delay: float = _RETRY_BASE_DELAY_SECONDS):
    """Run `op()` with exponential-backoff retry. Re-raises the last exception if all attempts fail."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await op()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is the generic retry boundary
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc


class JobQueue:
    """Thin wrapper around an ARQ pool: signature-verified webhooks call `enqueue_review`."""

    def __init__(self, redis: ArqRedis, breaker: CircuitBreaker | None = None) -> None:
        self._redis = redis
        self._breaker = breaker or CircuitBreaker()

    @classmethod
    async def connect(cls, redis_url: str) -> "JobQueue":
        settings = RedisSettings.from_dsn(redis_url)
        redis = ArqRedis(
            host=settings.host,
            port=settings.port,
            db=settings.database,
            username=settings.username,
            password=settings.password,
            ssl=settings.ssl,
            ssl_cert_reqs=settings.ssl_cert_reqs,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_COMMAND_TIMEOUT_SECONDS,
            encoding="utf8",
        )

        async def _ping() -> bool:
            return await redis.ping()

        await _with_retry(_ping)
        return cls(redis)

    async def close(self) -> None:
        await self._redis.close()

    async def _guarded(self, op):
        """Run `op` behind the circuit breaker, with retry-with-backoff, recording success/failure."""
        self._breaker.before_call()
        try:
            result = await _with_retry(op)
        except Exception:
            self._breaker.on_failure()
            raise
        self._breaker.on_success()
        return result

    async def mark_seen(self, delivery_id: str) -> bool:
        """Atomically claim a GitHub delivery id. Returns True the FIRST time it's seen,
        False on any replay (idempotency for retried/duplicated deliveries). If the
        caller subsequently fails to enqueue the job, it MUST call `unclaim` to release
        this so a genuine redelivery isn't silently swallowed."""

        async def _set_nx() -> bool | None:
            return await self._redis.set(
                f"webhook:delivery:{delivery_id}", "1", nx=True, ex=DELIVERY_DEDUP_TTL_SECONDS
            )

        return bool(await self._guarded(_set_nx))

    async def unclaim(self, delivery_id: str) -> None:
        """Release a dedup claim made by `mark_seen` after a failed enqueue, so a
        redelivery of the same id gets a real retry instead of being dropped."""

        async def _delete() -> None:
            await self._redis.delete(f"webhook:delivery:{delivery_id}")

        await self._guarded(_delete)

    async def enqueue_review(self, job: ReviewJob) -> str | None:
        """Enqueue a review job. Raises JobEnqueueError if it could not be enqueued
        after retries — callers must roll back any prior dedup claim on failure."""

        async def _enqueue():
            return await self._redis.enqueue_job("run_review", job.model_dump())

        try:
            arq_job = await self._guarded(_enqueue)
        except Exception as exc:
            raise JobEnqueueError(f"failed to enqueue review job for delivery {job.delivery_id}") from exc
        return arq_job.job_id if arq_job else None
