"""BudgetGuard (ADR-004): a hard daily spend cap on LLM usage.

Called at the top of every specialist agent run, *before* any span or LLM call.
It reads the day's running cost from the cost ledger and raises
`BudgetExceeded` if the cap is already reached — the review degrades to
"slower-but-correct, never fast-but-wrong": no partial spend past the limit.

M6 does a post-hoc check (`spend >= cap`), not a predictive one
(`spend + estimated_next_call > cap`). With four agents fanned out in parallel
the cap can be overshot by up to one in-flight call per agent; predictive
blocking is a later refinement.
"""
from __future__ import annotations

import os

from economics.cost_repository import SupportsSpendToday

_ENV_VAR = "DAILY_BUDGET_USD"
_DEFAULT_CAP_USD = 25.0


class BudgetExceeded(RuntimeError):
    """Raised by `BudgetGuard.check` when the daily LLM spend cap is reached."""


class BudgetGuard:
    def __init__(self, cost_repo: SupportsSpendToday, *, daily_cap_usd: float) -> None:
        self._cost_repo = cost_repo
        self._daily_cap_usd = daily_cap_usd

    @classmethod
    def from_env(cls, cost_repo: SupportsSpendToday, *, cap: float | None = None) -> "BudgetGuard":
        """`cap` (an explicit override, e.g. from a test) wins; otherwise read
        `DAILY_BUDGET_USD`; otherwise fall back to `_DEFAULT_CAP_USD`."""
        if cap is None:
            raw = os.environ.get(_ENV_VAR)
            cap = float(raw) if raw else _DEFAULT_CAP_USD
        return cls(cost_repo, daily_cap_usd=cap)

    @property
    def daily_cap_usd(self) -> float:
        return self._daily_cap_usd

    async def check(self, *, agent: str) -> None:
        spend = await self._cost_repo.spend_today_usd()
        if spend >= self._daily_cap_usd:
            raise BudgetExceeded(
                f"daily LLM budget of ${self._daily_cap_usd:.2f} reached "
                f"(spent ${spend:.4f}); blocking '{agent}' before its next call"
            )
