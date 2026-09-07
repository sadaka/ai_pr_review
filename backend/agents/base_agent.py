"""Base shape for a specialist agent: (BudgetGuard check →) retrieve grounding
context → call the LLM for a structured `SpecialistReviewDraft` → stamp
`agent_type` → return `Finding`s.

`outbound_call_safety`: the LLM call's timeout/retry is configured once on the
shared `AsyncOpenAI` client (see `agents/llm_client.py`), not hand-rolled per
call here — the SDK's built-in retry-with-backoff is well-tested for that.

Observability (M6): when a `review_id` and an event sink are supplied, the run
emits `span.start` / `tool.call` (retrieval) / `llm.call` (with token cost) /
`span.end` rows to the `agent_events` spine, and an injected budget guard is
consulted *before* any of that. All of it is opt-in — with no `review_id` the
pipeline is exactly the M4 path. `observability/**` is the documented exception
to `dependency_direction`; the budget guard is injected behind a local Protocol
so this module never imports `economics/**`.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from openai import AsyncOpenAI

from agents.contracts import AgentType, Finding, SpecialistReviewDraft
from agents.llm_client import usage_of
from memory.context_retriever import ContextRetriever, RetrievedChunk
from observability.events import (
    LLM_CALL,
    SPAN_END,
    SPAN_START,
    TOOL_CALL,
    NullEventSink,
    SupportsEmit,
)

DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_GROUNDING_K = 5


class SupportsBudgetCheck(Protocol):
    async def check(self, *, agent: str) -> None: ...


@dataclass(frozen=True)
class DiffContext:
    """The minimal input a specialist needs to review one PR."""

    repo_full_name: str
    pr_number: int
    diff_text: str


class SpecialistAgent:
    """Subclasses set `agent_type` and `system_prompt`; the review pipeline
    (retrieve -> prompt -> structured LLM call -> stamp -> validate) is
    shared, so a new specialist is just those two class attributes."""

    agent_type: AgentType
    system_prompt: str

    def __init__(
        self,
        llm: AsyncOpenAI,
        retriever: ContextRetriever,
        *,
        model: str = DEFAULT_MODEL,
        grounding_k: int = DEFAULT_GROUNDING_K,
        events: SupportsEmit | None = None,
        budget: SupportsBudgetCheck | None = None,
    ) -> None:
        self._llm = llm
        self._retriever = retriever
        self._model = model
        self._grounding_k = grounding_k
        self._events: SupportsEmit = events or NullEventSink()
        self._budget = budget

    async def review(self, diff: DiffContext, *, review_id: str | None = None) -> list[Finding]:
        # No review_id → the plain M4 path: no budget gate, no event emission.
        if review_id is None:
            grounding = await self._retrieve(diff)
            return await self._call_llm(diff, grounding)

        agent = self.agent_type.value

        # BudgetGuard runs first — before any span opens or any token is spent.
        if self._budget is not None:
            await self._budget.check(agent=agent)

        await self._events.emit(review_id=review_id, agent=agent, event_type=SPAN_START)
        span_started = perf_counter()
        outcome = "ok"
        try:
            tool_started = perf_counter()
            grounding = await self._retrieve(diff)
            await self._events.emit(
                review_id=review_id, agent=agent, event_type=TOOL_CALL, outcome="ok",
                latency_ms=int((perf_counter() - tool_started) * 1000),
                payload={"tool": "context_retriever", "k": self._grounding_k, "hits": len(grounding)},
            )

            llm_started = perf_counter()
            completion = await self._parse_completion(diff, grounding)
            tokens_in, tokens_out = usage_of(completion)
            await self._events.emit(
                review_id=review_id, agent=agent, event_type=LLM_CALL, model=self._model,
                tokens_in=tokens_in, tokens_out=tokens_out, outcome="ok",
                latency_ms=int((perf_counter() - llm_started) * 1000),
            )

            draft = completion.choices[0].message.parsed
            if draft is None:
                outcome = "empty"
                return []
            return [Finding.from_draft(f, agent_type=self.agent_type) for f in draft.findings]
        except BaseException:
            outcome = "error"
            raise
        finally:
            await self._events.emit(
                review_id=review_id, agent=agent, event_type=SPAN_END, outcome=outcome,
                latency_ms=int((perf_counter() - span_started) * 1000),
            )

    async def _retrieve(self, diff: DiffContext) -> list[RetrievedChunk]:
        return await self._retriever.retrieve(
            repo=diff.repo_full_name, query_text=diff.diff_text, k=self._grounding_k
        )

    async def _call_llm(self, diff: DiffContext, grounding: list[RetrievedChunk]) -> list[Finding]:
        completion = await self._parse_completion(diff, grounding)
        draft = completion.choices[0].message.parsed
        if draft is None:  # refused / couldn't parse — "nothing to report", not a crash
            return []
        return [Finding.from_draft(f, agent_type=self.agent_type) for f in draft.findings]

    async def _parse_completion(self, diff: DiffContext, grounding: list[RetrievedChunk]):
        return await self._llm.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self._build_prompt(diff, grounding)},
            ],
            response_format=SpecialistReviewDraft,
            temperature=0,
        )

    def _build_prompt(self, diff: DiffContext, grounding: list[RetrievedChunk]) -> str:
        if grounding:
            context_block = "\n\n".join(f"### {c.path}\n```\n{c.content}\n```" for c in grounding)
        else:
            context_block = "(no related codebase context retrieved)"

        return textwrap.dedent(
            f"""
            Repository: {diff.repo_full_name}
            Pull request: #{diff.pr_number}

            ## Diff under review
            ```diff
            {diff.diff_text}
            ```

            ## Retrieved codebase context (grounding — use this, don't guess)
            {context_block}
            """
        ).strip()
