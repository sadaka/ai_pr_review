"""Shared `AsyncOpenAI` client factory — the one place timeout/retry policy is
configured (`outbound_call_safety`). Every specialist agent and the context
retriever's embedding calls go through a client built here, not a bare
`AsyncOpenAI()`.
"""
from __future__ import annotations

from openai import AsyncOpenAI

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3  # exponential backoff, handled by the SDK


def create_llm_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)
