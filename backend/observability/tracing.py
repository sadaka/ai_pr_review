"""Trace viewer / audit reader: reconstruct one review from the events spine.

Spec §3.6 — "the trace viewer reconstructs any review with
`SELECT ... WHERE review_id = $1 ORDER BY ts`". The audit trail is the same
query; the table is append-only by construction.
"""
from __future__ import annotations

from typing import Any

import asyncpg  # type: ignore[import-untyped]


async def get_trace(pool: asyncpg.Pool, review_id: str) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT ts, agent, span_id, parent_span, event_type, model,
               tokens_in, tokens_out, cost_usd, latency_ms, outcome, confidence, payload
        FROM agent_events
        WHERE review_id = $1
        ORDER BY ts, event_type
        """,
        review_id,
    )
    return [dict(r) for r in rows]


def trace_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll a trace up to the facts M6's demo asserts on."""
    return {
        "agents": sorted({r["agent"] for r in rows}),
        "event_types": sorted({r["event_type"] for r in rows}),
        "llm_calls": sum(1 for r in rows if r["event_type"] == "llm.call"),
        "total_cost_usd": sum(float(r["cost_usd"]) for r in rows if r["cost_usd"] is not None),
    }
