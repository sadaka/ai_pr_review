"""Live golden-suite runner: for each case, seed its `repo_context` into
`code_chunks`, run the four real specialists over the diff, score the result,
and always delete the seeded rows afterward (never leave state in the shared
Tiger Cloud service).

This is the only part of the harness that needs credentials. It mirrors the
`seeded_repo` fixture in `tests/test_specialists_e2e.py`.
"""
from __future__ import annotations

import uuid

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no py.typed marker
from openai import AsyncOpenAI

from agents.base_agent import DiffContext
from agents.contracts import Finding
from agents.specialists import DocsAgent, QualityAgent, SecurityAgent, TestsAgent
from memory.context_retriever import ContextRetriever

from .golden_dataset import GoldenCase
from .scorer import SuiteScore, score_suite


async def _seed_context(pool: asyncpg.Pool, retriever: ContextRetriever, repo: str, case: GoldenCase) -> None:
    for index, rf in enumerate(case.repo_context):
        embedding = await retriever.embed(rf.content)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO code_chunks (repo, path, chunk_index, content, embedding)
                VALUES ($1, $2, $3, $4, $5)
                """,
                repo, rf.path, index, rf.content, embedding,
            )


async def _cleanup(pool: asyncpg.Pool, repo: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM code_chunks WHERE repo = $1", repo)


async def run_case(
    case: GoldenCase, *, pool: asyncpg.Pool, retriever: ContextRetriever, llm: AsyncOpenAI
) -> list[Finding]:
    repo = f"eval-golden/{case.id}-{uuid.uuid4().hex[:12]}"
    try:
        await _seed_context(pool, retriever, repo, case)
        diff = DiffContext(repo_full_name=repo, pr_number=1, diff_text=case.diff)
        agents = (
            SecurityAgent(llm, retriever),
            QualityAgent(llm, retriever),
            TestsAgent(llm, retriever),
            DocsAgent(llm, retriever),
        )
        findings: list[Finding] = []
        for agent in agents:
            findings.extend(await agent.review(diff))
        return findings
    finally:
        await _cleanup(pool, repo)


async def run_suite(
    cases: list[GoldenCase], *, pool: asyncpg.Pool, llm: AsyncOpenAI
) -> SuiteScore:
    retriever = ContextRetriever(pool, llm)
    pairs = [(case, await run_case(case, pool=pool, retriever=retriever, llm=llm)) for case in cases]
    return score_suite(pairs)
