"""M9: evaluation harness.

Deterministic tests (no credentials) cover the blocking path — golden-case
loading, the scorer's recall / false-positive maths, and the regression gate.
`test_golden_suite_e2e` is the one live test; it skips cleanly without
TIGER_DATABASE_URL / OPENAI_API_KEY, exactly like test_specialists_e2e.py.
"""
from __future__ import annotations

import os

import pytest

from agents.contracts import AgentType, Finding, Severity
from evaluation.golden_dataset import (
    ExpectedFinding,
    GoldenCase,
    RepoFile,
    load_golden_cases,
)
from evaluation.regression_gate import Baseline, GateThresholds, evaluate
from evaluation.scorer import CaseScore, SuiteScore, matches, score_case, score_suite


def _finding(
    *,
    agent: AgentType,
    file: str = "app/db.py",
    line: int | None = 13,
    category: str = "sql-injection",
    title: str = "Unparameterized query",
    rationale: str = "The query is built with an f-string, allowing SQL injection.",
) -> Finding:
    return Finding(
        agent_type=agent,
        severity=Severity.HIGH,
        category=category,
        file=file,
        line=line,
        confidence=0.9,
        title=title,
        rationale=rationale,
    )


# ── scorer ────────────────────────────────────────────────────────────────

def test_golden_recall_computed() -> None:
    case = GoldenCase(
        id="synthetic",
        description="two expected findings",
        diff="--- a/app/db.py\n+++ b/app/db.py\n",
        expected_findings=[
            ExpectedFinding(agent_type=AgentType.SECURITY, file="app/db.py", line=13,
                            rationale_contains="inject", note="sql injection"),
            ExpectedFinding(agent_type=AgentType.TESTS, file="app/db.py", note="missing test"),
        ],
        expect_max_findings={AgentType.SECURITY: 1},
    )
    actual = [
        _finding(agent=AgentType.SECURITY, line=14),                       # matches #1 (±tol)
        _finding(agent=AgentType.SECURITY, line=40, category="hardcoded"),  # extra security → FP (cap 1)
        _finding(agent=AgentType.DOCS, category="missing-docstring"),       # irrelevant
    ]
    score = score_case(case, actual)

    assert score.expected == 2
    assert score.matched == 1
    assert score.recall == 0.5
    assert score.missed == ["missing test"]
    assert score.false_positives == 1
    assert score.findings_by_agent == {"security": 2, "docs": 1}


def test_match_respects_line_tolerance_and_fields() -> None:
    expected = ExpectedFinding(
        agent_type=AgentType.SECURITY, file="app/db.py", line=10, line_tolerance=3,
        category_contains="inject",
    )
    assert matches(expected, _finding(agent=AgentType.SECURITY, line=12))
    assert not matches(expected, _finding(agent=AgentType.SECURITY, line=15))       # 5 > tol 3
    assert not matches(expected, _finding(agent=AgentType.SECURITY, file="app/x.py"))  # wrong file
    assert not matches(expected, _finding(agent=AgentType.TESTS, line=10))          # wrong agent
    assert not matches(
        expected, _finding(agent=AgentType.SECURITY, line=10, category="style")
    )  # category_contains not satisfied


def test_recall_is_one_when_nothing_expected() -> None:
    case = GoldenCase(id="benign", description="", diff="", expected_findings=[],
                      expect_max_findings={AgentType.SECURITY: 0})
    assert score_case(case, []).recall == 1.0
    assert score_case(case, [_finding(agent=AgentType.SECURITY)]).false_positives == 1


def test_load_golden_cases_parses_three() -> None:
    cases = load_golden_cases()
    assert len(cases) == 3
    by_id = {c.id for c in cases}
    assert by_id == {"sql_injection_lookup", "missing_tests_and_docs", "benign_refactor"}
    for c in cases:
        assert isinstance(c, GoldenCase)
        assert c.diff.strip()
        # every case constrains the specialists somehow
        assert c.expected_findings or c.expect_max_findings
    sql = next(c for c in cases if c.id == "sql_injection_lookup")
    assert any(e.agent_type == AgentType.SECURITY for e in sql.expected_findings)
    assert sql.repo_context and isinstance(sql.repo_context[0], RepoFile)


# ── regression gate ───────────────────────────────────────────────────────

def _suite(recall: float, fps: int) -> SuiteScore:
    # one synthetic case whose recall/FP we set directly
    matched = round(recall * 4)
    return SuiteScore(per_case=[CaseScore(
        case_id="s", expected=4, matched=matched, missed=[], false_positives=fps,
        findings_by_agent={},
    )])


BASE = Baseline(mean_recall=0.90, total_false_positives=0,
                recorded_at="2026-09-08T00:00:00+00:00", cases=["s"])


def test_regression_gate_blocks_on_drop() -> None:
    report = evaluate(_suite(recall=0.50, fps=0), BASE)
    assert report.passed is False
    assert any("recall" in r for r in report.reasons)


def test_regression_gate_passes_at_baseline() -> None:
    report = evaluate(_suite(recall=0.90, fps=0), BASE)
    assert report.passed is True
    assert report.reasons == []


def test_regression_gate_tolerates_small_recall_dip() -> None:
    # baseline 0.90 → current 0.875 is a real 0.025 drop, within the 0.05 tolerance → still passes.
    dipped = SuiteScore(per_case=[CaseScore(
        case_id="s", expected=8, matched=7, missed=["one"], false_positives=0, findings_by_agent={},
    )])  # recall 0.875
    report = evaluate(dipped, BASE, GateThresholds(max_recall_drop=0.05))
    assert report.passed is True
    # but a 0.01 tolerance would catch the same dip
    assert evaluate(dipped, BASE, GateThresholds(max_recall_drop=0.01)).passed is False


def test_regression_gate_blocks_on_new_false_positive() -> None:
    report = evaluate(_suite(recall=0.90, fps=2), BASE)
    assert report.passed is False
    assert any("false positive" in r for r in report.reasons)


def test_score_suite_aggregates() -> None:
    c1 = GoldenCase(id="a", description="", diff="x",
                    expected_findings=[ExpectedFinding(agent_type=AgentType.SECURITY, file="f", line=1)])
    c2 = GoldenCase(id="b", description="", diff="x", expected_findings=[],
                    expect_max_findings={AgentType.SECURITY: 0})
    suite = score_suite([
        (c1, [_finding(agent=AgentType.SECURITY, file="f", line=1)]),  # recall 1.0
        (c2, [_finding(agent=AgentType.SECURITY, file="f")]),          # recall 1.0, 1 FP
    ])
    assert suite.mean_recall == 1.0
    assert suite.total_false_positives == 1


# ── live e2e ──────────────────────────────────────────────────────────────

_LIVE = os.environ.get("TIGER_DATABASE_URL") and os.environ.get("OPENAI_API_KEY", "").startswith("sk-")


@pytest.mark.skipif(not _LIVE, reason="requires live TIGER_DATABASE_URL and OPENAI_API_KEY")
@pytest.mark.asyncio(loop_scope="module")
async def test_golden_suite_e2e() -> None:
    from agents.llm_client import create_llm_client
    from evaluation.judge import LLMJudge
    from evaluation.runner import run_case, run_suite
    from evaluation.scorer import matches as _matches
    from memory.context_retriever import ContextRetriever, create_pool

    cases = load_golden_cases()
    pool = await create_pool(os.environ["TIGER_DATABASE_URL"])
    try:
        llm = create_llm_client(os.environ["OPENAI_API_KEY"])
        suite = await run_suite(cases, pool=pool, llm=llm)

        assert suite.mean_recall >= 0.6, suite.model_dump()

        sql_case = next(c for c in cases if c.id == "sql_injection_lookup")
        sql_score = next(s for s in suite.per_case if s.case_id == "sql_injection_lookup")
        assert sql_score.matched >= 1, f"SQL-injection case missed everything: {sql_score.missed}"

        # non-blocking judge layer: score the security finding on the SQL case
        retriever = ContextRetriever(pool, llm)
        findings = await run_case(sql_case, pool=pool, retriever=retriever, llm=llm)
        sec_expected = next(e for e in sql_case.expected_findings if e.agent_type == AgentType.SECURITY)
        matched_sec = [f for f in findings if _matches(sec_expected, f)]
        assert matched_sec
        judge = LLMJudge(llm)
        rscore = await judge.score(matched_sec[0], sql_case)
        assert rscore.score >= 0.4, rscore.reasoning
    finally:
        await pool.close()
