"""Pure-logic coverage for the aggregator: dedup, scoring, the gate decision,
and body rendering. No DB, no network — the live truth-lane + HITL path is in
test_aggregator_hitl_e2e.py."""
from __future__ import annotations

import pytest

from agents.contracts import AgentType, Finding, Severity
from orchestrator.nodes import (
    Decision,
    aggregate,
    decide,
    dedup_findings,
    overall_confidence,
    render_review_body,
)


def _finding(
    *,
    agent: AgentType = AgentType.QUALITY,
    severity: Severity = Severity.MEDIUM,
    file: str = "app/db.py",
    line: int | None = 10,
    confidence: float = 0.9,
    title: str = "something",
    rationale: str = "because",
    category: str = "misc",
) -> Finding:
    return Finding(
        agent_type=agent,
        severity=severity,
        category=category,
        file=file,
        line=line,
        confidence=confidence,
        title=title,
        rationale=rationale,
    )


class _FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._next_id = 900_100

    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int:
        self._next_id += 1
        self.calls.append(
            {"repo_full_name": repo_full_name, "pr_number": pr_number, "body": body, "event": event}
        )
        return self._next_id


class _FakeTruthStore:
    """In-memory stand-in for integrations.truth_store.TruthStore."""

    def __init__(self) -> None:
        self.reviews: dict[str, dict] = {}
        self.findings: dict[str, list[Finding]] = {}
        self._seq = 0

    async def upsert_review(self, *, repo: str, pr_number: int, delivery_id: str):
        for r in self.reviews.values():
            if r["delivery_id"] == delivery_id:
                return _row(r)
        self._seq += 1
        rid = f"review-{self._seq}"
        self.reviews[rid] = {
            "id": rid,
            "delivery_id": delivery_id,
            "status": "pending",
            "github_review_id": None,
            "overall_confidence": None,
        }
        return _row(self.reviews[rid])

    async def insert_findings(self, *, review_id: str, findings) -> None:
        self.findings.setdefault(review_id, []).extend(findings)

    async def mark_posted(self, *, review_id: str, github_review_id: int, overall_confidence: float) -> None:
        self.reviews[review_id].update(
            status="posted", github_review_id=github_review_id, overall_confidence=overall_confidence
        )

    async def set_status(self, *, review_id: str, status: str, overall_confidence: float) -> None:
        self.reviews[review_id].update(status=status, overall_confidence=overall_confidence)


class _FakeHitl:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def enqueue(self, *, review_id: str, reason: str) -> str:
        hid = f"hitl-{len(self.rows) + 1}"
        self.rows.append({"id": hid, "review_id": review_id, "reason": reason})
        return hid


def _row(r: dict):
    from integrations.truth_store import ReviewRow

    return ReviewRow(id=r["id"], status=r["status"], github_review_id=r["github_review_id"])


# ── dedup ───────────────────────────────────────────────────────────────────


def test_dedup_keeps_highest_confidence_on_same_file_line():
    a = _finding(agent=AgentType.SECURITY, confidence=0.6, title="low")
    b = _finding(agent=AgentType.QUALITY, confidence=0.95, title="high")
    [kept] = dedup_findings([a, b])
    assert kept.title == "high"
    assert kept.confidence == 0.95


def test_dedup_notes_cross_agent_agreement_in_rationale():
    a = _finding(agent=AgentType.SECURITY, confidence=0.7, rationale="sql injection")
    b = _finding(agent=AgentType.QUALITY, confidence=0.9, rationale="unsafe query")
    [kept] = dedup_findings([a, b])
    assert "Also flagged by: security" in kept.rationale
    assert kept.rationale.startswith("unsafe query")


def test_dedup_leaves_distinct_locations_untouched():
    a = _finding(file="a.py", line=1)
    b = _finding(file="a.py", line=2)
    c = _finding(file="b.py", line=1)
    assert len(dedup_findings([a, b, c])) == 3


def test_dedup_orders_by_severity_then_file():
    low = _finding(severity=Severity.LOW, file="z.py", line=1)
    crit = _finding(severity=Severity.CRITICAL, file="a.py", line=9)
    med = _finding(severity=Severity.MEDIUM, file="m.py", line=1)
    assert [f.severity for f in dedup_findings([low, crit, med])] == [
        Severity.CRITICAL,
        Severity.MEDIUM,
        Severity.LOW,
    ]


# ── scoring ─────────────────────────────────────────────────────────────────


def test_overall_confidence_is_mean():
    fs = [_finding(confidence=0.8, line=1), _finding(confidence=0.6, line=2)]
    assert overall_confidence(fs) == pytest.approx(0.7)


def test_overall_confidence_empty_is_one():
    assert overall_confidence([]) == 1.0


# ── the gate ────────────────────────────────────────────────────────────────


def test_decide_auto_posts_when_confident_and_no_critical():
    fs = [_finding(severity=Severity.HIGH, confidence=0.9)]
    assert decide(fs, 0.9) is Decision.AUTO_POST


def test_decide_queues_when_below_threshold():
    fs = [_finding(severity=Severity.MEDIUM, confidence=0.5)]
    assert decide(fs, 0.5) is Decision.QUEUED


def test_decide_escalates_on_critical_even_at_high_confidence():
    fs = [_finding(severity=Severity.CRITICAL, confidence=0.99)]
    assert decide(fs, 0.99) is Decision.ESCALATED


def test_decide_empty_review_auto_posts():
    assert decide([], 1.0) is Decision.AUTO_POST


# ── body ────────────────────────────────────────────────────────────────────


def test_render_body_empty_is_approving():
    assert "No issues found" in render_review_body([])


def test_render_body_groups_by_severity_and_shows_location():
    body = render_review_body(
        dedup_findings(
            [
                _finding(severity=Severity.CRITICAL, file="db.py", line=42, title="sqli"),
                _finding(severity=Severity.LOW, file="x.py", line=None, title="nit"),
            ]
        )
    )
    assert "### CRITICAL" in body and "### LOW" in body
    assert "`db.py:42`" in body
    assert "`x.py`" in body  # line=None → bare file
    assert body.index("### CRITICAL") < body.index("### LOW")


# ── aggregate() wiring, fully faked ─────────────────────────────────────────


async def test_aggregate_auto_post_path():
    gh, ts, hitl = _FakeGitHub(), _FakeTruthStore(), _FakeHitl()
    res = await aggregate(
        repo_full_name="octo/repo",
        pr_number=7,
        delivery_id="d-auto",
        findings_by_agent=[[_finding(severity=Severity.HIGH, confidence=0.9)], []],
        github=gh,
        truth_store=ts,
        hitl=hitl,
    )
    assert res.decision is Decision.AUTO_POST
    assert len(gh.calls) == 1
    assert gh.calls[0]["event"] == "COMMENT"
    assert res.github_review_id is not None
    assert hitl.rows == []
    assert ts.reviews[res.review_id]["status"] == "posted"


async def test_aggregate_empty_review_posts_approve_event():
    gh, ts, hitl = _FakeGitHub(), _FakeTruthStore(), _FakeHitl()
    res = await aggregate(
        repo_full_name="octo/repo",
        pr_number=7,
        delivery_id="d-empty",
        findings_by_agent=[[], [], [], []],
        github=gh,
        truth_store=ts,
        hitl=hitl,
    )
    assert res.decision is Decision.AUTO_POST
    assert gh.calls[0]["event"] == "APPROVE"


async def test_aggregate_low_confidence_queues_without_posting():
    gh, ts, hitl = _FakeGitHub(), _FakeTruthStore(), _FakeHitl()
    res = await aggregate(
        repo_full_name="octo/repo",
        pr_number=8,
        delivery_id="d-low",
        findings_by_agent=[[_finding(severity=Severity.MEDIUM, confidence=0.5)]],
        github=gh,
        truth_store=ts,
        hitl=hitl,
    )
    assert res.decision is Decision.QUEUED
    assert gh.calls == []
    assert len(hitl.rows) == 1
    assert "below threshold" in hitl.rows[0]["reason"]
    assert ts.reviews[res.review_id]["status"] == "needs_human"


async def test_aggregate_critical_escalates_without_posting():
    gh, ts, hitl = _FakeGitHub(), _FakeTruthStore(), _FakeHitl()
    res = await aggregate(
        repo_full_name="octo/repo",
        pr_number=9,
        delivery_id="d-crit",
        findings_by_agent=[[_finding(severity=Severity.CRITICAL, confidence=0.98, title="rce")]],
        github=gh,
        truth_store=ts,
        hitl=hitl,
    )
    assert res.decision is Decision.ESCALATED
    assert gh.calls == []
    assert len(hitl.rows) == 1
    assert "CRITICAL" in hitl.rows[0]["reason"] and "rce" in hitl.rows[0]["reason"]
    assert ts.reviews[res.review_id]["status"] == "escalated"


async def test_aggregate_redelivery_does_not_double_post():
    gh, ts, hitl = _FakeGitHub(), _FakeTruthStore(), _FakeHitl()
    kw = dict(
        repo_full_name="octo/repo",
        pr_number=10,
        delivery_id="d-dup",
        findings_by_agent=[[_finding(severity=Severity.HIGH, confidence=0.9)]],
        github=gh,
        truth_store=ts,
        hitl=hitl,
    )
    first = await aggregate(**kw)  # type: ignore[arg-type]
    second = await aggregate(**kw)  # type: ignore[arg-type]
    assert len(gh.calls) == 1
    assert first.review_id == second.review_id
    assert second.decision is Decision.AUTO_POST
