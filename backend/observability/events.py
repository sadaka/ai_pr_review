"""The events spine (ADR-003 lane 2 / spec §3.6): every agent action becomes
one append-only row in the `agent_events` hypertable — span start/end, LLM
call, tool call, decision — carrying the `span_id`/`parent_span` chain, cost,
latency, confidence and outcome. Three consumers read that one table: the trace
viewer (`tracing.py`), the audit trail (the table itself, immutable by
construction), and the cost ledger (`economics/`).

`dependency_direction`: `observability/**` is the documented exception to the
inward-only rule — it is cross-cutting middleware, injected into the agents and
the orchestrator, and may be imported from anywhere. It imports
`economics.pricing` (a pure helper) to stamp `cost_usd` on `llm.call` rows.

`single_data_spine`: writes go to the one Tiger Cloud store, never Redis.
`outbound_call_safety`: the pool carries a `command_timeout`; retry/circuit
breaker on the Tiger connection is the L8/L12 reliability gate (same ruling as
M4/M5 — the invariant's own text defers it).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no py.typed marker

from economics.pricing import cost_usd as _cost_usd

_COMMAND_TIMEOUT_SECONDS = 10.0  # outbound_call_safety

# event_type vocabulary (matches the migration comment on agent_events)
SPAN_START = "span.start"
SPAN_END = "span.end"
LLM_CALL = "llm.call"
TOOL_CALL = "tool.call"
DECISION = "decision"


class SupportsEmit(Protocol):
    async def emit(
        self,
        *,
        review_id: str,
        agent: str,
        event_type: str,
        span_id: str | None = ...,
        parent_span: str | None = ...,
        model: str | None = ...,
        tokens_in: int | None = ...,
        tokens_out: int | None = ...,
        cost_usd: float | None = ...,
        latency_ms: int | None = ...,
        outcome: str | None = ...,
        confidence: float | None = ...,
        payload: Mapping[str, Any] | None = ...,
    ) -> None: ...


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        database_url, command_timeout=_COMMAND_TIMEOUT_SECONDS, min_size=1, max_size=5
    )


class AgentEventSink:
    """Writes rows to `agent_events`. One instance per process, sharing an
    asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> "AgentEventSink":
        return cls(await create_pool(database_url))

    async def close(self) -> None:
        await self._pool.close()

    async def emit(
        self,
        *,
        review_id: str,
        agent: str,
        event_type: str,
        span_id: str | None = None,
        parent_span: str | None = None,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        outcome: str | None = None,
        confidence: float | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        # Stamp cost on llm.call rows when the caller passed tokens but no cost.
        if cost_usd is None and event_type == LLM_CALL and model is not None:
            cost_usd = _cost_usd(model, tokens_in or 0, tokens_out or 0)

        await self._pool.execute(
            """
            INSERT INTO agent_events
                (ts, review_id, agent, span_id, parent_span, event_type, model,
                 tokens_in, tokens_out, cost_usd, latency_ms, outcome, confidence, payload)
            VALUES (now(), $1, $2,
                    COALESCE($3::uuid, gen_random_uuid()), $4::uuid, $5, $6,
                    $7, $8, $9, $10, $11, $12, $13::jsonb)
            """,
            review_id,
            agent,
            span_id,
            parent_span,
            event_type,
            model,
            tokens_in,
            tokens_out,
            cost_usd,
            latency_ms,
            outcome,
            confidence,
            json.dumps(dict(payload)) if payload is not None else None,
        )


class NullEventSink:
    """No-op sink. Lets a `SpecialistAgent` run with observability switched off
    (unit tests, or any caller that has no Tiger connection) without branching
    on `events is None` everywhere."""

    async def emit(
        self,
        *,
        review_id: str,
        agent: str,
        event_type: str,
        span_id: str | None = None,
        parent_span: str | None = None,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        outcome: str | None = None,
        confidence: float | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        return None
