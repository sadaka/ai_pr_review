"""A shared behavioural contract for circuit breakers.

`job_queue/queue.py` and `integrations/github_client.py` predate the
`reliability/` module and keep their own breaker implementations (working,
independently tested). Rather than force a risky refactor, this contract pins
all of them — plus `reliability.CircuitBreaker` — to the same observable
behaviour: open after N consecutive failures, fast-fail while open, half-open
after the cooldown, close on a probe success.
"""
from __future__ import annotations

from typing import Callable, Protocol


class BreakerLike(Protocol):
    def before_call(self) -> None: ...
    def on_success(self) -> None: ...
    def on_failure(self) -> None: ...


def assert_breaker_contract(make_breaker: Callable[[int, float], BreakerLike]) -> None:
    """`make_breaker(threshold, reset_after_seconds)` builds a fresh breaker."""
    # 1. closed breaker allows calls
    b = make_breaker(2, 999.0)
    b.before_call()  # no raise

    # 2. one failure below threshold: still closed
    b.on_failure()
    b.before_call()  # no raise

    # 3. hitting the threshold opens it -> before_call raises
    b.on_failure()
    raised = False
    try:
        b.before_call()
    except Exception:  # noqa: BLE001 - each impl has its own CircuitOpenError type
        raised = True
    assert raised, f"{make_breaker!r}: breaker did not open after 2 consecutive failures"

    # 4. a success resets the failure count (non-consecutive failures don't stack)
    b = make_breaker(2, 999.0)
    b.on_failure()
    b.on_success()
    b.on_failure()
    b.before_call()  # still closed - no raise

    # 5. cooldown of 0 -> immediately half-open, probe allowed through
    b = make_breaker(1, 0.0)
    b.on_failure()
    b.before_call()  # no raise: the probe is allowed
    b.on_success()
    b.before_call()  # fully closed again
