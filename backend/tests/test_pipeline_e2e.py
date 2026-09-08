"""M11: the real end-to-end pipeline.

`test_pipeline_fake_end_to_end` drives a ReviewJob through `run_review` with
the 4 real SpecialistAgents wired to a fake LLM + fake retriever, a fake
GitHub, and in-memory truth/hitl/events — proving the wiring (webhook job →
diff fetch → LangGraph fan-out → nodes.aggregate → truth write + one post).
`test_pipeline_live_end_to_end` does the same against real OpenAI + Tiger and
skips without creds.
"""
from __future__ import annotations

import itertools
import os
import uuid

import fakeredis
import pytest

from typing import Any

from agents.base_agent import SpecialistAgent
from agents.contracts import FindingDraft, Severity, SpecialistReviewDraft
from agents.specialists import DocsAgent, QualityAgent, SecurityAgent, TestsAgent
from integrations.github_client import GitHubAppClient
from job_queue.arq_worker import run_review
from orchestrator.graph import GraphDeps
from orchestrator.langgraph_engine import LangGraphEngine

CANNED_DIFF = """--- a/app/db.py
+++ b/app/db.py
@@ -10,2 +10,6 @@ def get_user_by_id(user_id):
     return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
+
+def get_user_by_name(username):
+    query = f"SELECT * FROM users WHERE name = '{username}'"
+    return conn.execute(query).fetchone()
"""


# ── fakes ─────────────────────────────────────────────────────────────────

class _Parsed:
    def __init__(self, draft: SpecialistReviewDraft) -> None:
        self.message = type("M", (), {"parsed": draft})()


class _Completion:
    def __init__(self, draft: SpecialistReviewDraft) -> None:
        self.choices = [_Parsed(draft)]
        self.usage = None


class _FakeCompletions:
    def __init__(self, lines: "itertools.count") -> None:
        self._lines = lines

    async def parse(self, **kwargs: object) -> _Completion:
        line = next(self._lines)
        draft = SpecialistReviewDraft(
            findings=[
                FindingDraft(
                    severity=Severity.HIGH,
                    category="sql-injection",
                    file="app/db.py",
                    line=line,
                    confidence=0.9,
                    title=f"Unparameterized query (line {line})",
                    rationale="Query built with an f-string — SQL injection.",
                )
            ]
        )
        return _Completion(draft)


class _FakeLLM:
    def __init__(self) -> None:
        self.chat = type("C", (), {"completions": _FakeCompletions(itertools.count(11))})()


class _FakeRetriever:
    async def retrieve(self, *, repo: str, query_text: str, k: int = 5) -> list:
        return []


class _FakeGitHub:
    def __init__(self) -> None:
        self.diff_calls: list[tuple[str, int]] = []
        self.post_calls: list[dict] = []

    async def get_pull_request_diff(self, *, repo_full_name: str, pr_number: int) -> str:
        self.diff_calls.append((repo_full_name, pr_number))
        return CANNED_DIFF

    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int:
        self.post_calls.append({"repo": repo_full_name, "pr": pr_number, "event": event})
        return 555


class _Row:
    def __init__(self, rid: str, status: str, ghid: int | None) -> None:
        self.id = rid
        self.status = status
        self.github_review_id = ghid


class _FakeTruthStore:
    def __init__(self) -> None:
        self.reviews: dict[str, _Row] = {}  # delivery_id -> row
        self.findings: dict[str, list] = {}

    async def upsert_review(self, *, repo: str, pr_number: int, delivery_id: str) -> _Row:
        return self.reviews.setdefault(delivery_id, _Row(f"rev-{delivery_id}", "pending", None))

    async def insert_findings(self, *, review_id: str, findings) -> None:
        # ON CONFLICT DO NOTHING semantics: dedup on (agent_type, file, line, category)
        bucket = self.findings.setdefault(review_id, [])
        seen = {(f.agent_type, f.file, f.line, f.category) for f in bucket}
        for f in findings:
            key = (f.agent_type, f.file, f.line, f.category)
            if key not in seen:
                bucket.append(f)
                seen.add(key)

    async def mark_posted(self, *, review_id: str, github_review_id: int, overall_confidence: float) -> None:
        for row in self.reviews.values():
            if row.id == review_id:
                row.status = "posted"
                row.github_review_id = github_review_id

    async def set_status(self, *, review_id: str, status: str, overall_confidence: float) -> None:
        for row in self.reviews.values():
            if row.id == review_id:
                row.status = status


class _FakeHitl:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def enqueue(self, *, review_id: str, reason: str) -> str:
        self.rows.append({"review_id": review_id, "reason": reason})
        return f"hitl-{len(self.rows)}"


class _CapturingEvents:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, **kwargs: object) -> None:
        self.events.append(kwargs)


def _agents(llm: Any, retriever: Any, events: Any) -> dict[str, SpecialistAgent]:
    return {
        "security": SecurityAgent(llm, retriever, events=events),
        "quality": QualityAgent(llm, retriever, events=events),
        "tests": TestsAgent(llm, retriever, events=events),
        "docs": DocsAgent(llm, retriever, events=events),
    }


def _build_ctx() -> tuple[dict, _FakeGitHub, _FakeTruthStore, _FakeHitl, _CapturingEvents]:
    llm = _FakeLLM()
    retriever = _FakeRetriever()
    events = _CapturingEvents()
    agents = _agents(llm, retriever, events)
    github = _FakeGitHub()
    truth = _FakeTruthStore()
    hitl = _FakeHitl()
    engine = LangGraphEngine(
        fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()),
        deps=GraphDeps(agents=agents, github=github, truth_store=truth, hitl=hitl, events=events),
    )
    ctx = {"github": github, "truth_store": truth, "engine": engine}
    return ctx, github, truth, hitl, events


def _job(delivery_id: str) -> dict:
    return {
        "delivery_id": delivery_id,
        "repo_full_name": "octocat/hello-world",
        "pr_number": 7,
        "action": "opened",
    }


# ── tests ─────────────────────────────────────────────────────────────────

async def test_pipeline_fake_end_to_end() -> None:
    ctx, github, truth, hitl, events = _build_ctx()

    result = await run_review(ctx, _job("d-1"))

    assert github.diff_calls == [("octocat/hello-world", 7)]
    # 4 specialists each produced a finding on a distinct line → 4 survive dedup
    assert result["findings"] == 4
    assert result["decision"] == "auto_post"
    assert result["review_id"] == "rev-d-1"
    # exactly one review posted, nothing queued for a human
    assert len(github.post_calls) == 1
    assert hitl.rows == []
    # the truth lane has the review + its findings
    assert truth.reviews["d-1"].status == "posted"
    assert len(truth.findings["rev-d-1"]) == 4
    # observability: every agent emitted a span + the aggregator a decision
    kinds = [e.get("event_type") for e in events.events]
    assert kinds.count("span.start") == 4
    assert "decision" in kinds


async def test_pipeline_redelivery_is_idempotent() -> None:
    ctx, github, truth, hitl, events = _build_ctx()

    first = await run_review(ctx, _job("d-2"))
    second = await run_review(ctx, _job("d-2"))

    assert first["decision"] == "auto_post"
    assert second["replayed"] is True
    assert second["decision"] == "auto_post"
    # the redelivery did not fetch the diff again, re-run agents, or re-post
    assert github.diff_calls == [("octocat/hello-world", 7)]
    assert len(github.post_calls) == 1
    assert len(truth.findings["rev-d-2"]) == 4


async def test_specialist_node_serializes_findings_through_state() -> None:
    import json

    from orchestrator.graph import _make_real_specialist_node

    graph_deps = GraphDeps(
        agents={"security": SecurityAgent(_FakeLLM(), _FakeRetriever())},  # type: ignore[arg-type]
        github=_FakeGitHub(),
        truth_store=_FakeTruthStore(),
        hitl=_FakeHitl(),
    )
    node = _make_real_specialist_node("security", graph_deps)
    out = await node(
        {"repo_full_name": "o/r", "pr_number": 1, "diff_text": CANNED_DIFF, "review_id": "x"}, {}
    )
    # the node's output must round-trip through JSON (LangGraph checkpoints it to Redis)
    assert json.loads(json.dumps(out)) == out
    assert out["specialist_results"][0]["agent"] == "security"
    assert out["specialist_results"][0]["findings"][0]["file"] == "app/db.py"


async def test_get_pull_request_diff_requests_raw_media_type() -> None:
    import httpx

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("Accept")
        seen["path"] = request.url.path
        return httpx.Response(200, text=CANNED_DIFF)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="https://api.github.com", transport=transport)
    client = GitHubAppClient(app_id="1", private_key_pem="unused", http_client=http)
    # short-circuit the auth chain
    client._installation_tokens[42] = ("tok", 10**12)
    client._installation_ids["octocat/hello-world"] = 42

    diff = await client.get_pull_request_diff(repo_full_name="octocat/hello-world", pr_number=7)

    assert diff == CANNED_DIFF
    assert seen["accept"] == "application/vnd.github.v3.diff"
    assert seen["path"] == "/repos/octocat/hello-world/pulls/7"
    await http.aclose()


# ── live ──────────────────────────────────────────────────────────────────

_LIVE = os.environ.get("TIGER_DATABASE_URL") and os.environ.get("OPENAI_API_KEY", "").startswith("sk-")


@pytest.mark.skipif(not _LIVE, reason="requires live TIGER_DATABASE_URL and OPENAI_API_KEY")
@pytest.mark.asyncio(loop_scope="module")
async def test_pipeline_live_end_to_end() -> None:
    from agents.llm_client import create_llm_client
    from hitl.queue import HitlQueue
    from integrations.truth_store import TruthStore
    from memory.context_retriever import ContextRetriever, create_pool
    from observability.events import AgentEventSink

    pool = await create_pool(os.environ["TIGER_DATABASE_URL"])
    delivery = f"m11-live-{uuid.uuid4().hex[:12]}"
    try:
        llm = create_llm_client(os.environ["OPENAI_API_KEY"])
        retriever = ContextRetriever(pool, llm)
        events = AgentEventSink(pool)
        agents = _agents(llm, retriever, events)
        github = _FakeGitHub()  # canned diff in, post_review recorded — no real GitHub PR fixture
        truth = TruthStore(pool)
        hitl = HitlQueue(pool)
        engine = LangGraphEngine(
            fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()),
            deps=GraphDeps(agents=agents, github=github, truth_store=truth, hitl=hitl, events=events),
        )
        ctx = {"github": github, "truth_store": truth, "engine": engine}

        result = await run_review(ctx, _job(delivery))
        review_id = result["review_id"]

        assert result["findings"] >= 1
        assert result["decision"] in {"auto_post", "queued", "escalated"}
        finding_count = await pool.fetchval(
            "SELECT count(*) FROM finding_records WHERE review_id = $1", review_id
        )
        assert finding_count >= 1
        event_count = await pool.fetchval(
            "SELECT count(*) FROM agent_events WHERE review_id = $1", review_id
        )
        assert event_count >= 4  # at least one span per specialist
        if result["decision"] == "auto_post":
            assert len(github.post_calls) == 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM finding_records WHERE review_id IN "
                "(SELECT id FROM pr_review_records WHERE github_delivery_id = $1)",
                delivery,
            )
            await conn.execute(
                "DELETE FROM hitl_reviews WHERE review_id IN "
                "(SELECT id FROM pr_review_records WHERE github_delivery_id = $1)",
                delivery,
            )
            await conn.execute(
                "DELETE FROM agent_events WHERE review_id IN "
                "(SELECT id FROM pr_review_records WHERE github_delivery_id = $1)",
                delivery,
            )
            await conn.execute("DELETE FROM pr_review_records WHERE github_delivery_id = $1", delivery)
        await pool.close()
