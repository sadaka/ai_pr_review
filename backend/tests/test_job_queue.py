"""Unit coverage for job_queue: dedup semantics + circuit breaker, independent
of the webhook HTTP layer (test_webhook_ingress.py covers that with a fake)."""
from __future__ import annotations

import fakeredis
import pytest

from job_queue.queue import CircuitBreaker, CircuitOpenError, JobQueue


@pytest.fixture
def fake_redis():
    return fakeredis.FakeAsyncRedis()


async def test_mark_seen_true_first_time_false_on_replay(fake_redis):
    queue = JobQueue(fake_redis)

    first = await queue.mark_seen("delivery-abc")
    second = await queue.mark_seen("delivery-abc")

    assert first is True
    assert second is False


async def test_mark_seen_distinct_ids_both_true(fake_redis):
    queue = JobQueue(fake_redis)

    assert await queue.mark_seen("delivery-1") is True
    assert await queue.mark_seen("delivery-2") is True


class _FailsTwiceThenSucceedsRedis:
    """Simulates a transient blip: the first two calls fail, the third succeeds."""

    def __init__(self) -> None:
        self.calls = 0

    async def set(self, *args, **kwargs):
        self.calls += 1
        if self.calls < 3:
            raise ConnectionError("simulated transient redis blip")
        return True


async def test_mark_seen_retries_transient_failures_then_succeeds():
    redis = _FailsTwiceThenSucceedsRedis()
    queue = JobQueue(redis)

    result = await queue.mark_seen("delivery-retry")

    assert result is True
    assert redis.calls == 3  # proves retry-with-backoff actually retried, not just one shot


class _AlwaysFailsRedis:
    async def set(self, *args, **kwargs):
        raise ConnectionError("simulated redis down")


async def test_circuit_breaker_opens_after_threshold_failures():
    queue = JobQueue(_AlwaysFailsRedis(), breaker=CircuitBreaker(failure_threshold=2, reset_after_seconds=999))

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await queue.mark_seen("x")

    # third call: breaker is open, fails fast without hitting redis again
    with pytest.raises(CircuitOpenError):
        await queue.mark_seen("x")


async def test_circuit_breaker_half_opens_after_cooldown():
    breaker = CircuitBreaker(failure_threshold=1, reset_after_seconds=0)
    queue = JobQueue(_AlwaysFailsRedis(), breaker=breaker)

    with pytest.raises(ConnectionError):
        await queue.mark_seen("x")

    # cooldown is 0s, so the breaker should immediately allow a probe call through
    # (which will fail again against the always-failing double, but as ConnectionError,
    # not CircuitOpenError — proving the probe was attempted).
    with pytest.raises(ConnectionError):
        await queue.mark_seen("x")
