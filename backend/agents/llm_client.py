"""Shared `AsyncOpenAI` client factory — the one place timeout/retry policy is
configured (`outbound_call_safety`). Every specialist agent and the context
retriever's embedding calls go through a client built here, not a bare
`AsyncOpenAI()`.
"""
from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3  # exponential backoff, handled by the SDK


def create_llm_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)


def usage_of(completion: Any) -> tuple[int, int]:
    """`(prompt_tokens, completion_tokens)` from a chat completion, defensively
    — a mocked client or a provider that omits usage yields `(0, 0)` rather
    than an AttributeError. Feeds cost attribution on the events spine."""
    usage = getattr(completion, "usage", None)
    if usage is None:
        return (0, 0)
    return (int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0))
