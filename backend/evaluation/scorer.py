"""Deterministic scoring: given a golden case and the `Finding`s the real
specialists produced, compute recall of the expected findings and a count of
false positives (findings beyond a case's `expect_max_findings` ceiling).

This is the blocking metric. No LLM, no network — pure functions over
`Finding` objects, so `tests/test_evaluation.py` exercises every branch with
synthetic inputs and no credentials.
"""
from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict

from agents.contracts import Finding

from .golden_dataset import ExpectedFinding, GoldenCase


def matches(expected: ExpectedFinding, finding: Finding) -> bool:
    if finding.agent_type != expected.agent_type:
        return False
    if finding.file != expected.file:
        return False
    if expected.line is not None and finding.line is not None:
        if abs(finding.line - expected.line) > expected.line_tolerance:
            return False
    if expected.category_contains is not None:
        if expected.category_contains.lower() not in finding.category.lower():
            return False
    if expected.rationale_contains is not None:
        haystack = f"{finding.title}\n{finding.rationale}".lower()
        if expected.rationale_contains.lower() not in haystack:
            return False
    return True


class CaseScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    expected: int
    matched: int
    missed: list[str]
    false_positives: int
    findings_by_agent: dict[str, int]

    @property
    def recall(self) -> float:
        return 1.0 if self.expected == 0 else self.matched / self.expected


class SuiteScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_case: list[CaseScore]

    @property
    def mean_recall(self) -> float:
        if not self.per_case:
            return 1.0
        return sum(c.recall for c in self.per_case) / len(self.per_case)

    @property
    def total_false_positives(self) -> int:
        return sum(c.false_positives for c in self.per_case)


def score_case(case: GoldenCase, actual: list[Finding]) -> CaseScore:
    remaining = list(actual)
    matched = 0
    missed: list[str] = []
    for expected in case.expected_findings:
        hit = next((f for f in remaining if matches(expected, f)), None)
        if hit is None:
            missed.append(case.label(expected))
        else:
            remaining.remove(hit)
            matched += 1

    by_agent = Counter(f.agent_type.value for f in actual)
    false_positives = 0
    for agent, cap in case.expect_max_findings.items():
        false_positives += max(0, by_agent.get(agent.value, 0) - cap)

    return CaseScore(
        case_id=case.id,
        expected=len(case.expected_findings),
        matched=matched,
        missed=missed,
        false_positives=false_positives,
        findings_by_agent=dict(by_agent),
    )


def score_suite(pairs: list[tuple[GoldenCase, list[Finding]]]) -> SuiteScore:
    return SuiteScore(per_case=[score_case(case, findings) for case, findings in pairs])
