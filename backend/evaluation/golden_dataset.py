"""Golden dataset: hand-authored PR review cases with the findings a competent
reviewer should surface. Cases live as YAML in `evaluation/golden/*.yaml` so
the embedded code and diffs read naturally (block scalars, not escaped JSON).

An expected finding is a *partial* match spec over the deterministic fields of
`Finding` — `agent_type`, `file`, roughly `line`, and substrings of
`category` / `rationale`. It deliberately never constrains `title` or the full
`rationale` prose: those are LLM output and not reproducible.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agents.contracts import AgentType

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


class RepoFile(BaseModel):
    """One file seeded into `code_chunks` as grounding context before the case
    runs — the established-pattern code a grounded specialist contrasts against."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


class ExpectedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_type: AgentType
    file: str
    line: int | None = None
    line_tolerance: int = 3
    category_contains: str | None = None
    rationale_contains: str | None = None
    note: str | None = Field(default=None, description="human label, only for the `missed` report")


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    repo_context: list[RepoFile] = []
    diff: str
    expected_findings: list[ExpectedFinding] = []
    # Per-agent ceiling on finding count. An agent absent from the map is
    # unconstrained; `security: 0` means "this diff has no security issue,
    # any security finding is a false positive".
    expect_max_findings: dict[AgentType, int] = {}

    def label(self, expected: ExpectedFinding) -> str:
        return expected.note or f"{expected.agent_type.value}:{expected.file}:{expected.line}"


def load_golden_cases(path: Path = GOLDEN_DIR) -> list[GoldenCase]:
    """Every `*.yaml` under `path`, sorted by filename for a stable suite order."""
    files = sorted(path.glob("*.yaml"))
    if not files:
        raise FileNotFoundError(f"no golden cases found under {path}")
    return [GoldenCase.model_validate(yaml.safe_load(f.read_text())) for f in files]
