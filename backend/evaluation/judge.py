"""LLM-as-judge: a non-blocking second layer that scores how well a matched
finding's rationale actually explains a real problem. It never gates CI — the
deterministic scorer does that — so a flaky or costly judge call can't wedge
the pipeline. Runs only in the live e2e, over findings the scorer already
matched.

Reuses the shared `AsyncOpenAI` client (its timeout/retry policy), consistent
with `outbound_call_safety`.
"""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agents.contracts import Finding

from .golden_dataset import GoldenCase

JUDGE_MODEL = "gpt-5.4-mini"

JUDGE_SYSTEM = (
    "You grade code-review findings. Given a PR diff and one finding a reviewer "
    "raised about it, rate from 0.0 to 1.0 how well the finding's rationale "
    "identifies and explains a genuine problem in the diff. 1.0 = precise, "
    "correct, well-grounded; 0.0 = vague, wrong, or hallucinated. Be strict."
)


class RationaleScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


class SupportsStructuredCompletion(Protocol):
    @property
    def chat(self) -> Any: ...


class LLMJudge:
    def __init__(self, llm: SupportsStructuredCompletion, *, model: str = JUDGE_MODEL) -> None:
        self._llm = llm
        self._model = model

    async def score(self, finding: Finding, case: GoldenCase) -> RationaleScore:
        user = (
            f"## PR diff\n{case.diff}\n\n"
            f"## Finding ({finding.agent_type.value}, {finding.severity.value})\n"
            f"file: {finding.file}:{finding.line}\n"
            f"category: {finding.category}\n"
            f"title: {finding.title}\n"
            f"rationale: {finding.rationale}\n"
        )
        completion = await self._llm.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format=RationaleScore,
            temperature=0,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            return RationaleScore(score=0.0, reasoning="judge returned no parseable score")
        return parsed
