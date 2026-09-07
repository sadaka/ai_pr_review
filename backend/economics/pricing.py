"""Approximate OpenAI token pricing, keyed by model.

USD per 1,000,000 tokens, as (input, output). These are ballpark figures for
cost *attribution* on the events spine and the daily BudgetGuard — not billing.
Verify against live pricing (https://openai.com/api/pricing/) before treating
any number here as authoritative.
"""
from __future__ import annotations

# (input_usd_per_1M, output_usd_per_1M)
PRICE_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-5.4-mini": (0.15, 0.60),
    "gpt-5.4": (2.50, 10.00),
    "text-embedding-3-small": (0.02, 0.0),
}

_DEFAULT: tuple[float, float] = (0.50, 1.50)  # unknown model — conservative middle


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Dollar cost of one model call. Unknown models fall back to `_DEFAULT`
    rather than raising — a missing price must not break a review."""
    price_in, price_out = PRICE_PER_1M.get(model, _DEFAULT)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000
