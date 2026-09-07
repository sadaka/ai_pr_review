"""M6 demo: pytest backend/tests/test_observability_budget_e2e.py -k
"full_review_traces_to_agent_events and budget_guard_blocks_over_cap"

Live events spine: real Tiger Cloud Postgres — `agent_events` rows are actually
written and read back through the trace viewer. The LLM and the retriever are
faked (canned parsed output + usage; no grounding), so the test is deterministic
and free — M6 is about the spine, not the agents. Skips cleanly when
TIGER_DATABASE_URL is unset.

Each test uses a unique `review_id` (a real uuid) and deletes its own
`agent_events` rows afterward — this runs against the shared dev service.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from agents.base_agent import DiffContext
from agents.contracts import AgentType, FindingDraft, Severity, SpecialistReviewDraft
from agents.specialists.docs import DocsAgent
from agents.specialists.quality import QualityAgent
from agents.specialists.security import SecurityAgent
from agents.specialists.tests import TestsAgent
from economics.budget import BudgetExceeded, BudgetGuard
from economics.cost_repository import CostRepository
from observability.events import AgentEventSink, create_pool
from observability.tracing import get_trace, trace_summary
from orchestrator.nodes import Decision, aggregate

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TIGER_DATABASE_URL"),
        reason="requires live TIGER_DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

SPECIALIST_CLASSES = (SecurityAgent, QualityAgent, TestsAgent, DocsAgent)
DIFF = DiffContext(repo_full_name="test-fixture/m6", pr_number=1, diff_text="+ x = 1")


# ── fakes ───────────────────────────────────────────────────────────────────


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Message:
    def __init__(self, parsed: SpecialistReviewDraft | None) -> None:
        self.parsed = parsed


class _Choice:
    def __init__(self, parsed: SpecialistReviewDraft | None) -> None:
        self.message = _Message(parsed)


class _Completion:
    def __init__(self, parsed: SpecialistReviewDraft | None) -> None:
        self.choices = [_Choice(parsed)]
        self.usage = _Usage(1200, 300)


class _Completions:
    def __init__(self, parsed: SpecialistReviewDraft | None) -> None:
        self._parsed = parsed
        self.called = False

    async def parse(self, **_: object) -> _Completion:
        self.called = True
        return _Completion(self._parsed)


class FakeLLM:
    def __init__(self, parsed: SpecialistReviewDraft | None) -> None:
        self.completions = _Completions(parsed)
        self.chat = type("_Chat", (), {"completions": self.completions})()


class FakeRetriever:
    async def retrieve(self, *, repo: str, query_text: str, k: int = 5) -> list:
        return []


def _one_finding_draft(agent: AgentType) -> SpecialistReviewDraft:
    return SpecialistReviewDraft(
        findings=[
            FindingDraft(
                severity=Severity.MEDIUM,
                category="misc",
                file="app/db.py",
                line=42,
                confidence=0.9,
                title=f"{agent.value} finding",
                rationale="rationale",
            )
        ]
    )


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool():
    p = await create_pool(os.environ["TIGER_DATABASE_URL"])
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="module")
async def spine(pool):
    """(sink, cost_repo, cleanup) sharing the live pool. `track(review_id)`
    registers rows for FK-free deletion on teardown."""
    created: list[str] = []
    sink = AgentEventSink(pool)
    cost_repo = CostRepository(pool)

    class _Spine:
        def __init__(self) -> None:
            self.sink = sink
            self.cost_repo = cost_repo

        def track(self, review_id: str) -> None:
            created.append(review_id)

    yield _Spine()

    for review_id in created:
        await pool.execute("DELETE FROM agent_events WHERE review_id = $1", review_id)


class _FakeTruthStore:
    """aggregate() only needs upsert_review → a pending row, plus no-op writes.
    We don't exercise the truth lane here (M5 covers it)."""

    def __init__(self, review_id: str) -> None:
        self._review_id = review_id

    async def upsert_review(self, *, repo: str, pr_number: int, delivery_id: str):
        return type("_Row", (), {"id": self._review_id, "status": "pending", "github_review_id": None})()

    async def insert_findings(self, *, review_id: str, findings) -> None:
        return None

    async def mark_posted(self, *, review_id: str, github_review_id: int, overall_confidence: float) -> None:
        return None

    async def set_status(self, *, review_id: str, status: str, overall_confidence: float) -> None:
        return None


class _FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int:
        self.calls.append({"event": event})
        return 900_001


class _FakeHitl:
    async def enqueue(self, *, review_id: str, reason: str) -> str:
        return "hitl-fake"


# ── tests ───────────────────────────────────────────────────────────────────


async def test_full_review_traces_to_agent_events(spine, pool):
    review_id = str(uuid.uuid4())
    spine.track(review_id)

    findings_by_agent = []
    for cls in SPECIALIST_CLASSES:
        agent = cls(
            FakeLLM(_one_finding_draft(cls.agent_type)),
            FakeRetriever(),
            events=spine.sink,
        )
        findings_by_agent.append(await agent.review(DIFF, review_id=review_id))

    result = await aggregate(
        repo_full_name=DIFF.repo_full_name,
        pr_number=DIFF.pr_number,
        delivery_id=f"m6-{review_id[:12]}",
        findings_by_agent=findings_by_agent,
        github=_FakeGitHub(),
        truth_store=_FakeTruthStore(review_id),
        hitl=_FakeHitl(),
        events=spine.sink,
    )
    assert result.decision is Decision.AUTO_POST

    rows = await get_trace(pool, review_id)
    summary = trace_summary(rows)

    assert {"span.start", "span.end", "tool.call", "llm.call", "decision"}.issubset(summary["event_types"])
    assert {"security", "quality", "tests", "docs", "aggregator"}.issubset(summary["agents"])
    assert summary["llm_calls"] == 4
    # every llm.call row carries an attributed cost
    llm_rows = [r for r in rows if r["event_type"] == "llm.call"]
    assert all(r["cost_usd"] is not None and float(r["cost_usd"]) > 0 for r in llm_rows)
    # rows come back in time order
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)


async def test_budget_guard_blocks_over_cap(spine, pool):
    review_id = str(uuid.uuid4())
    spine.track(review_id)

    # A real, dated spend row: $1.00 today.
    await spine.sink.emit(
        review_id=review_id, agent="security", event_type="llm.call",
        model="gpt-5.4-mini", tokens_in=0, tokens_out=0, cost_usd=1.0,
    )

    spent = await spine.cost_repo.spend_today_usd()
    assert spent >= 1.0

    guard = BudgetGuard(spine.cost_repo, daily_cap_usd=0.01)  # far below today's spend
    with pytest.raises(BudgetExceeded):
        await guard.check(agent="quality")

    # and an agent given that guard never reaches its LLM call
    llm = FakeLLM(_one_finding_draft(AgentType.QUALITY))
    agent = QualityAgent(llm, FakeRetriever(), events=spine.sink, budget=guard)
    with pytest.raises(BudgetExceeded):
        await agent.review(DIFF, review_id=review_id)
    assert llm.completions.called is False


async def test_full_review_traces_to_agent_events_and_budget_guard_blocks_over_cap(spine, pool):
    """The M6 demo: the spine records a full review, and the BudgetGuard hard-
    blocks once the day's spend is over the cap — both in one run."""
    review_id = str(uuid.uuid4())
    spine.track(review_id)

    # 1. a full traced review
    findings_by_agent = []
    for cls in SPECIALIST_CLASSES:
        agent = cls(FakeLLM(_one_finding_draft(cls.agent_type)), FakeRetriever(), events=spine.sink)
        findings_by_agent.append(await agent.review(DIFF, review_id=review_id))
    await aggregate(
        repo_full_name=DIFF.repo_full_name, pr_number=DIFF.pr_number,
        delivery_id=f"m6-combo-{review_id[:12]}", findings_by_agent=findings_by_agent,
        github=_FakeGitHub(), truth_store=_FakeTruthStore(review_id), hitl=_FakeHitl(),
        events=spine.sink,
    )
    summary = trace_summary(await get_trace(pool, review_id))
    assert {"span.start", "span.end", "tool.call", "llm.call", "decision"}.issubset(summary["event_types"])
    assert {"security", "quality", "tests", "docs", "aggregator"}.issubset(summary["agents"])
    assert summary["total_cost_usd"] > 0

    # 2. that review's own cost now pushes the day over a tiny cap
    guard = BudgetGuard(spine.cost_repo, daily_cap_usd=1e-9)
    blocked_llm = FakeLLM(_one_finding_draft(AgentType.SECURITY))
    blocked = SecurityAgent(blocked_llm, FakeRetriever(), events=spine.sink, budget=guard)
    with pytest.raises(BudgetExceeded):
        await blocked.review(DIFF, review_id=review_id)
    assert blocked_llm.completions.called is False
