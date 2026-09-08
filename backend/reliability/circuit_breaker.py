"""Fail-fast circuit breaker: after N consecutive failures the breaker opens
and calls are rejected without touching the downstream dependency; after a
cooldown it half-opens to let one probe through, and a success closes it again.

This is the canonical shape that `job_queue/queue.py::CircuitBreaker` and
`integrations/github_client.py::_CircuitBreaker` each implement independently.
`conformance.assert_breaker_contract` pins all three to the same behaviour.
"""
from __future__ import annotations

import time

DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_RESET_AFTER_SECONDS = 30.0


class CircuitOpenError(RuntimeError):
    """Raised instead of attempting the call while the breaker is open."""


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        reset_after_seconds: float = DEFAULT_RESET_AFTER_SECONDS,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._reset_after = reset_after_seconds
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self._reset_after:
            return "half_open"
        return "open"

    def before_call(self) -> None:
        if self.state == "open":
            raise CircuitOpenError(f"{self.name} circuit breaker is open")

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()
