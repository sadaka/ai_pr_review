"""Unit coverage for SpecialistAgent.review(): fully mocked LLM + retriever,
no network calls. The live-LLM/live-DB path is exercised separately by
test_specialists_e2e.py."""
from __future__ import annotations

from agents.base_agent import DiffContext, SpecialistAgent
from agents.contracts import AgentType, FindingDraft, Severity, SpecialistReviewDraft
from memory.context_retriever import RetrievedChunk


class _FakeMessage:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed):
        self.message = _FakeMessage(parsed)


class _FakeCompletion:
    def __init__(self, parsed):
        self.choices = [_FakeChoice(parsed)]


class _FakeCompletions:
    def __init__(self, parsed_result):
        self._parsed_result = parsed_result
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeCompletion(self._parsed_result)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class FakeLLM:
    def __init__(self, parsed_result: SpecialistReviewDraft | None) -> None:
        self.completions = _FakeCompletions(parsed_result)
        self.chat = _FakeChat(self.completions)


class FakeRetriever:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self._chunks = chunks
        self.calls: list[dict] = []

    async def retrieve(self, *, repo: str, query_text: str, k: int = 5) -> list[RetrievedChunk]:
        self.calls.append({"repo": repo, "query_text": query_text, "k": k})
        return self._chunks


class _StubAgent(SpecialistAgent):
    agent_type = AgentType.QUALITY
    system_prompt = "you are a stub quality reviewer"


DIFF = DiffContext(repo_full_name="octocat/hello-world", pr_number=1, diff_text="+ x = 1")


async def test_review_stamps_agent_type_on_every_finding():
    draft = SpecialistReviewDraft(
        findings=[
            FindingDraft(
                severity=Severity.MEDIUM,
                category="unused-variable",
                file="app.py",
                line=3,
                confidence=0.7,
                title="unused var",
                rationale="x is never read",
            )
        ]
    )
    llm = FakeLLM(draft)
    retriever = FakeRetriever([])

    findings = await _StubAgent(llm, retriever).review(DIFF)

    assert len(findings) == 1
    assert findings[0].agent_type == AgentType.QUALITY
    assert findings[0].file == "app.py"


async def test_review_returns_empty_list_when_llm_returns_none():
    llm = FakeLLM(None)
    retriever = FakeRetriever([])

    findings = await _StubAgent(llm, retriever).review(DIFF)

    assert findings == []


async def test_review_passes_diff_text_as_retrieval_query_scoped_to_repo():
    llm = FakeLLM(SpecialistReviewDraft(findings=[]))
    retriever = FakeRetriever([])

    await _StubAgent(llm, retriever, grounding_k=3).review(DIFF)

    assert retriever.calls == [{"repo": "octocat/hello-world", "query_text": "+ x = 1", "k": 3}]


async def test_review_includes_grounding_and_diff_in_prompt():
    llm = FakeLLM(SpecialistReviewDraft(findings=[]))
    retriever = FakeRetriever([RetrievedChunk(path="app/db.py", content="def q(): ...", score=0.9)])

    await _StubAgent(llm, retriever).review(DIFF)

    [call] = llm.completions.calls
    user_message = call["messages"][1]["content"]
    assert "+ x = 1" in user_message
    assert "app/db.py" in user_message
    assert "def q(): ..." in user_message
    assert call["messages"][0]["content"] == "you are a stub quality reviewer"
    assert call["response_format"] is SpecialistReviewDraft


async def test_review_no_grounding_says_so_explicitly_in_prompt():
    llm = FakeLLM(SpecialistReviewDraft(findings=[]))
    retriever = FakeRetriever([])

    await _StubAgent(llm, retriever).review(DIFF)

    [call] = llm.completions.calls
    assert "no related codebase context retrieved" in call["messages"][1]["content"]
