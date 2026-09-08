"""Shared `AsyncOpenAI` client factory — the one place timeout/retry policy is
configured (`outbound_call_safety`). Every specialist agent and the context
retriever's embedding calls go through a client built here, not a bare
`AsyncOpenAI()`.
"""
from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from reliability import LLM_TIMEOUT_SECONDS, CircuitBreaker, Guard

REQUEST_TIMEOUT_SECONDS = LLM_TIMEOUT_SECONDS
MAX_RETRIES = 3  # exponential backoff, handled by the SDK

# One process-wide breaker for the OpenAI chat endpoint: the SDK already does
# retry-with-backoff per call, so `Guard` adds attempts=1 + an outer timeout +
# the breaker — after repeated hard failures the next call fails fast instead
# of every specialist agent hanging for 30s in turn.
_CHAT_BREAKER = CircuitBreaker(name="openai-chat")
_chat_guard = Guard(breaker=_CHAT_BREAKER, attempts=1, timeout_seconds=LLM_TIMEOUT_SECONDS)


def create_llm_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)


async def guarded_parse(llm: Any, **kwargs: Any) -> Any:
    """`llm.chat.completions.parse(**kwargs)` behind the shared OpenAI breaker.
    Specialist agents and the eval judge call this instead of the raw client so
    a GPT outage trips one breaker for the whole process."""
    return await _chat_guard(
        lambda: llm.chat.completions.parse(**kwargs), name="openai.chat.completions.parse"
    )


def usage_of(completion: Any) -> tuple[int, int]:
    """`(prompt_tokens, completion_tokens)` from a chat completion, defensively
    — a mocked client or a provider that omits usage yields `(0, 0)` rather
    than an AttributeError. Feeds cost attribution on the events spine."""
    usage = getattr(completion, "usage", None)
    if usage is None:
        return (0, 0)
    return (int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0))
