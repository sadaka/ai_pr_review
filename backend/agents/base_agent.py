"""Base shape for a specialist agent: retrieve grounding context, call the LLM
for a structured `SpecialistReviewDraft`, stamp `agent_type`, return `Finding`s.

`outbound_call_safety`: the LLM call's timeout/retry is configured once on the
shared `AsyncOpenAI` client (see `agents/llm_client.py`), not hand-rolled per
call here — the SDK's built-in retry-with-backoff is well-tested or that.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass

from openai import AsyncOpenAI

from agents.contracts import AgentType, Finding, SpecialistReviewDraft
from memory.context_retriever import ContextRetriever, RetrievedChunk

DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_GROUNDING_K = 5


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
    ) -> None:
        self._llm = llm
        self._retriever = retriever
        self._model = model
        self._grounding_k = grounding_k

    async def review(self, diff: DiffContext) -> list[Finding]:
        grounding = await self._retriever.retrieve(
            repo=diff.repo_full_name, query_text=diff.diff_text, k=self._grounding_k
        )
        prompt = self._build_prompt(diff, grounding)

        completion = await self._llm.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            response_format=SpecialistReviewDraft,
            temperature=0,
        )
        draft = completion.choices[0].message.parsed
        if draft is None:  # refused / couldn't parse — treat as "nothing to report", not a crash
            return []
        return [Finding.from_draft(f, agent_type=self.agent_type) for f in draft.findings]

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
