"""The L8/L12 reliability mechanics (spec §4.2 module map: retry, circuit
breaker, idempotency, timeout).

Pure leaf module — imports nothing else under `backend/`. It sits at the same
tier as `core/`, so every other module (`integrations/`, `economics/`,
`memory/`, `agents/`, `job_queue/`) imports *inward* to it and the
`dependency_direction` invariant holds with no cross-cutting exception.

`Guard` composes the three call-site concerns in the right order — circuit
breaker → retry-with-backoff → timeout — so a caller wraps an outbound
operation once instead of hand-rolling all three (as `job_queue/queue.py` and
`integrations/github_client.py` each did before this module existed; those keep
their own copies, now pinned to a shared behavioural contract by
`conformance.assert_breaker_contract`).
"""
from __future__ import annotations

from .circuit_breaker import CircuitBreaker, CircuitOpenError
from .guard import Guard
from .idempotency import OnceGuard, fingerprint
from .retry import retry_async
from .timeout import (
    DB_COMMAND_TIMEOUT_SECONDS,
    GITHUB_TIMEOUT_SECONDS,
    LLM_TIMEOUT_SECONDS,
    REDIS_TIMEOUT_SECONDS,
    OperationTimeout,
    with_timeout,
)

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "Guard",
    "OnceGuard",
    "fingerprint",
    "retry_async",
    "with_timeout",
    "OperationTimeout",
    "DB_COMMAND_TIMEOUT_SECONDS",
    "GITHUB_TIMEOUT_SECONDS",
    "LLM_TIMEOUT_SECONDS",
    "REDIS_TIMEOUT_SECONDS",
]
