from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.contracts import AgentType, Finding, FindingDraft, Severity, SpecialistReviewDraft


def test_finding_from_draft_stamps_agent_type():
    draft = FindingDraft(
        severity=Severity.HIGH,
        category="sql-injection",
        file="app/db.py",
        line=42,
        confidence=0.9,
        title="Unsanitized query",
        rationale="user input reaches the query unescaped",
    )
    finding = Finding.from_draft(draft, agent_type=AgentType.SECURITY)

    assert finding.agent_type == AgentType.SECURITY
    assert finding.file == "app/db.py"
    assert finding.line == 42


def test_finding_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        FindingDraft(
            severity=Severity.LOW,
            category="x",
            file="a.py",
            confidence=1.5,  # out of [0, 1]
            title="t",
            rationale="r",
        )


def test_finding_draft_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        FindingDraft(
            severity=Severity.LOW,
            category="x",
            file="a.py",
            confidence=0.5,
            title="t",
            rationale="r",
            agent_type="security",  # not part of the draft schema — must be rejected
        )


def test_specialist_review_draft_empty_findings_is_valid():
    review = SpecialistReviewDraft(findings=[])
    assert review.findings == []
