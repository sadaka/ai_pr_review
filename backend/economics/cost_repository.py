"""The cost ledger: reads dollar spend off the events spine.

The dashboard reads the continuous aggregates (`agent_health_1m`,
`pr_cost_hourly`) — those are kept warm by Tiger and are the right source for a
UI that must not scan raw rows on every load. The BudgetGuard, however, needs
the *current* running total including calls made seconds ago, and the
continuous aggregates lag their refresh policy (~1 min). So this repository sums
raw `agent_events` directly. Same store, two read paths, different freshness
needs.

`outbound_call_safety`: pool carries `command_timeout`; retry/breaker deferred
to L8/L12 per that invariant's own text.
"""
from __future__ import annotations

from typing import Protocol

import asyncpg  # type: ignore[import-untyped]


class SupportsSpendToday(Protocol):
    async def spend_today_usd(self) -> float: ...


class CostRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def spend_today_usd(self) -> float:
        """Total `cost_usd` across all agents since the start of the current
        UTC day. `date_trunc('day', ts)` truncates in the session timezone, so
        the boundary is pinned to UTC explicitly rather than trusting the Tiger
        session's `TimeZone` setting."""
        value = await self._pool.fetchval(
            """
            SELECT COALESCE(SUM(cost_usd), 0)::float8
            FROM agent_events
            WHERE ts >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            """
        )
        return float(value)

    async def spend_for_review_usd(self, review_id: str) -> float:
        value = await self._pool.fetchval(
            """
            SELECT COALESCE(SUM(cost_usd), 0)::float8
            FROM agent_events
            WHERE review_id = $1
            """,
            review_id,
        )
        return float(value)
