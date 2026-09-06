"""The Finding contract (`structured_output_only` invariant): every specialist
agent returns `Finding` objects validated against this schema — never raw
prose. The aggregator (M5) merges data, not text.

Two-tier design: `FindingDraft` is what the LLM is asked to produce (it
doesn't know which agent it is — that would be redundant and an unnecessary
thing to trust the model to get right). `Finding` is the real contract,
constructed by `base_agent` from a validated draft plus the agent's own
identity, stamped programmatically rather than trusted from LLM output.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class AgentType(str, Enum):
    SECURITY = "security"
    QUALITY = "quality"
    TESTS = "tests"
    DOCS = "docs"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingDraft(BaseModel):
    """What the LLM produces via structured output — one call, one agent, so
    `agent_type` is deliberately not part of this schema."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    category: str = Field(description="short kebab-case label, e.g. 'sql-injection', 'missing-test-coverage'")
    file: str = Field(description="path of the file this finding is about, as it appears in the diff")
    line: int | None = Field(default=None, description="1-indexed line number in the NEW file version, if known")
    confidence: float = Field(ge=0.0, le=1.0)
    title: str = Field(description="one-line summary, shown to the reviewer")
    rationale: str = Field(description="why this matters, grounded in the retrieved codebase context if used")


class SpecialistReviewDraft(BaseModel):
    """Top-level structured-output object — OpenAI's structured outputs need a
    single object, not a bare list, hence the wrapper."""

    model_config = ConfigDict(extra="forbid")

    findings: list[FindingDraft]


class Finding(BaseModel):
    """The real contract: everything downstream (aggregator, HITL queue,
    GitHub posting — M5+) consumes this, never `FindingDraft` or raw text."""

    model_config = ConfigDict(extra="forbid")

    agent_type: AgentType
    severity: Severity
    category: str
    file: str
    line: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    title: str
    rationale: str

    @classmethod
    def from_draft(cls, draft: FindingDraft, *, agent_type: AgentType) -> "Finding":
        return cls(agent_type=agent_type, **draft.model_dump())
