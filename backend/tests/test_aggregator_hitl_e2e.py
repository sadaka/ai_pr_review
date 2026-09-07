"""M5 demo: pytest backend/tests/test_aggregator_hitl_e2e.py -k
"high_confidence_auto_posts and low_confidence_queues and critical_always_escalates"

Live truth lane: real Tiger Cloud Postgres — `pr_review_records`,
`finding_records`, `hitl_reviews` are actually written and read back. GitHub is
faked (a recorder), so nothing is posted to a real repo. Skips cleanly when
TIGER_DATABASE_URL is unset.

Every test uses a unique `delivery_id` and deletes its own rows afterward —
this runs against the shared dev service.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from agents.contracts import AgentType, Finding, Severity
from hitl.queue import HitlQueue
from integrations.truth_store import TruthStore, create_pool
from orchestrator.nodes import Decision, aggregate

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TIGER_DATABASE_URL"),
        reason="requires live TIGER_DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


class FakeGitHub:
    """Stands in for integrations.github_client.GitHubAppClient — records the
    call instead of hitting api.github.com."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._next_id = 500_000

    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int:
        self._next_id += 1
        self.calls.append(
            {"repo_full_name": repo_full_name, "pr_number": pr_number, "body": body, "event": event}
        )
        return self._next_id


def _finding(
    *,
    agent: AgentType,
    severity: Severity,
    confidence: float,
    file: str = "app/db.py",
    line: int = 42,
    title: str = "finding",
    rationale: str = "rationale",
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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool():
    p = await create_pool(os.environ["TIGER_DATABASE_URL"])
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="module")
async def env(pool):
    """A fresh (truth_store, hitl, github, cleanup) per test. `cleanup` deletes
    every review this test created, FK-safe, on teardown."""
    created: list[str] = []

    truth_store = TruthStore(pool)
    hitl = HitlQueue(pool)
    github = FakeGitHub()

    class _Env:
        def __init__(self) -> None:
            self.truth_store = truth_store
            self.hitl = hitl
            self.github = github

        def track(self, review_id: str) -> None:
            created.append(review_id)

    yield _Env()

    for review_id in created:
        await pool.execute("DELETE FROM hitl_reviews WHERE review_id = $1", review_id)
        await pool.execute("DELETE FROM finding_records WHERE review_id = $1", review_id)
        await pool.execute("DELETE FROM pr_review_records WHERE id = $1", review_id)


async def _run(env, *, delivery_id: str, pr_number: int, findings_by_agent):
    result = await aggregate(
        repo_full_name="test-fixture/m5",
        pr_number=pr_number,
        delivery_id=delivery_id,
        findings_by_agent=findings_by_agent,
        github=env.github,
        truth_store=env.truth_store,
        hitl=env.hitl,
    )
    env.track(result.review_id)
    return result


async def _review_row(pool, review_id: str):
    return await pool.fetchrow(
        "SELECT status, overall_confidence, github_review_id FROM pr_review_records WHERE id = $1",
        review_id,
    )


async def test_high_confidence_auto_posts(env, pool):
    result = await _run(
        env,
        delivery_id=f"m5-auto-{uuid.uuid4().hex[:12]}",
        pr_number=1,
        findings_by_agent=[
            [_finding(agent=AgentType.QUALITY, severity=Severity.MEDIUM, confidence=0.9)],
            [_finding(agent=AgentType.DOCS, severity=Severity.LOW, confidence=0.85, line=7)],
        ],
    )

    assert result.decision is Decision.AUTO_POST
    assert len(env.github.calls) == 1
    assert result.github_review_id is not None

    row = await _review_row(pool, result.review_id)
    assert row["status"] == "posted"
    assert row["github_review_id"] == result.github_review_id
    assert await env.hitl.pending_count(review_id=result.review_id) == 0

    findings = await pool.fetch("SELECT * FROM finding_records WHERE review_id = $1", result.review_id)
    assert len(findings) == 2


async def test_low_confidence_queues(env, pool):
    result = await _run(
        env,
        delivery_id=f"m5-low-{uuid.uuid4().hex[:12]}",
        pr_number=2,
        findings_by_agent=[
            [_finding(agent=AgentType.QUALITY, severity=Severity.MEDIUM, confidence=0.5)],
            [_finding(agent=AgentType.TESTS, severity=Severity.LOW, confidence=0.45, line=8)],
        ],
    )

    assert result.decision is Decision.QUEUED
    assert env.github.calls == []
    assert result.github_review_id is None

    row = await _review_row(pool, result.review_id)
    assert row["status"] == "needs_human"
    assert await env.hitl.pending_count(review_id=result.review_id) == 1

    hitl_row = await pool.fetchrow(
        "SELECT reason, status FROM hitl_reviews WHERE review_id = $1", result.review_id
    )
    assert hitl_row["status"] == "pending"
    assert "threshold" in hitl_row["reason"]


async def test_critical_always_escalates(env, pool):
    # High confidence — the CRITICAL must escalate anyway.
    result = await _run(
        env,
        delivery_id=f"m5-crit-{uuid.uuid4().hex[:12]}",
        pr_number=3,
        findings_by_agent=[
            [_finding(agent=AgentType.SECURITY, severity=Severity.CRITICAL, confidence=0.97, title="SQL injection")],
            [_finding(agent=AgentType.QUALITY, severity=Severity.LOW, confidence=0.95, line=5)],
        ],
    )

    assert result.decision is Decision.ESCALATED
    assert result.overall_confidence > 0.75  # confidence alone would have auto-posted
    assert env.github.calls == []

    row = await _review_row(pool, result.review_id)
    assert row["status"] == "escalated"
    assert await env.hitl.pending_count(review_id=result.review_id) == 1

    reason = await pool.fetchval(
        "SELECT reason FROM hitl_reviews WHERE review_id = $1", result.review_id
    )
    assert "CRITICAL" in reason and "SQL injection" in reason


async def test_redelivery_is_idempotent(env, pool):
    delivery_id = f"m5-dup-{uuid.uuid4().hex[:12]}"
    findings = [[_finding(agent=AgentType.QUALITY, severity=Severity.MEDIUM, confidence=0.9)]]

    first = await _run(env, delivery_id=delivery_id, pr_number=4, findings_by_agent=findings)
    second = await _run(env, delivery_id=delivery_id, pr_number=4, findings_by_agent=findings)

    assert first.review_id == second.review_id
    assert len(env.github.calls) == 1  # not two
    findings_rows = await pool.fetch(
        "SELECT id FROM finding_records WHERE review_id = $1", first.review_id
    )
    assert len(findings_rows) == 1  # findings not inserted twice


async def test_high_confidence_auto_posts_and_low_confidence_queues_and_critical_always_escalates(env, pool):
    """The M5 demo: all three routing outcomes in one run, distinct deliveries."""
    auto = await _run(
        env,
        delivery_id=f"m5-combo-auto-{uuid.uuid4().hex[:12]}",
        pr_number=11,
        findings_by_agent=[[_finding(agent=AgentType.QUALITY, severity=Severity.MEDIUM, confidence=0.92)]],
    )
    low = await _run(
        env,
        delivery_id=f"m5-combo-low-{uuid.uuid4().hex[:12]}",
        pr_number=12,
        findings_by_agent=[[_finding(agent=AgentType.DOCS, severity=Severity.LOW, confidence=0.4)]],
    )
    crit = await _run(
        env,
        delivery_id=f"m5-combo-crit-{uuid.uuid4().hex[:12]}",
        pr_number=13,
        findings_by_agent=[[_finding(agent=AgentType.SECURITY, severity=Severity.CRITICAL, confidence=0.99, title="RCE")]],
    )

    # high confidence, no CRITICAL → posted once, nothing queued
    assert auto.decision is Decision.AUTO_POST
    assert len(env.github.calls) == 1
    assert (await _review_row(pool, auto.review_id))["status"] == "posted"
    assert await env.hitl.pending_count(review_id=auto.review_id) == 0

    # low confidence → not posted, one hitl row
    assert low.decision is Decision.QUEUED
    assert (await _review_row(pool, low.review_id))["status"] == "needs_human"
    assert await env.hitl.pending_count(review_id=low.review_id) == 1

    # any CRITICAL → not posted regardless of confidence, escalation row
    assert crit.decision is Decision.ESCALATED
    assert (await _review_row(pool, crit.review_id))["status"] == "escalated"
    assert await env.hitl.pending_count(review_id=crit.review_id) == 1

    # still exactly one GitHub call across all three
    assert len(env.github.calls) == 1
