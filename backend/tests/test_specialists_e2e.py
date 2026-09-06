"""M4 demo: pytest backend/tests/test_specialists_e2e.py -k
"seeded_repo_diff_produces_valid_findings"

Genuinely live: real OpenAI embeddings + chat completions, real Tiger Cloud
Postgres. Costs a handful of cents and a few seconds per run. Skips cleanly
if TIGER_DATABASE_URL / OPENAI_API_KEY aren't set (e.g. CI without secrets),
rather than failing — this is the one test file in the suite that requires
live credentials; everything else (test_base_agent.py etc.) is fully mocked.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from agents.base_agent import DiffContext
from agents.contracts import Finding
from agents.llm_client import create_llm_client
from agents.specialists import DocsAgent, QualityAgent, SecurityAgent, TestsAgent
from memory.context_retriever import ContextRetriever, create_pool

# The module-scoped async fixtures below (the asyncpg pool especially) must be
# created and used on the SAME event loop — pytest-asyncio's default per-function
# loop would otherwise leave the pool "attached to a different loop". Pin the
# whole module (fixtures + tests) to one module-scoped loop.
pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get("TIGER_DATABASE_URL") and os.environ.get("OPENAI_API_KEY", "").startswith("sk-")),
        reason="requires live TIGER_DATABASE_URL and OPENAI_API_KEY",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

# A small fixture "repo": an existing correctly-parameterized query (the
# established pattern) alongside the new, unparameterized one under review —
# the retrieved context includes the good pattern so a grounded security
# agent can meaningfully contrast against it.
EXISTING_DB_MODULE = '''
import sqlite3

DB_PATH = "app.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def get_user_by_id(user_id):
    """Look up a user by id using a parameterized query."""
    conn = get_connection()
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
'''.strip()

# The diff under review: adds a SECOND lookup function that builds its query
# with an f-string (SQL injection), has no test, and no docstring — gives all
# four specialists something real and distinct to find.
VULNERABLE_DIFF = '''--- a/app/db.py
+++ b/app/db.py
@@ -10,3 +10,7 @@ def get_user_by_id(user_id):
     conn = get_connection()
     return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
+
+def get_user_by_name(username):
+    conn = get_connection()
+    query = f"SELECT * FROM users WHERE name = '{username}'"
+    return conn.execute(query).fetchone()
'''

VULNERABLE_FILE = "app/db.py"


@pytest.fixture(scope="module")
def fixture_repo() -> str:
    return f"test-fixture/m4-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db_pool():
    pool = await create_pool(os.environ["TIGER_DATABASE_URL"])
    yield pool
    await pool.close()


@pytest.fixture(scope="module")
def llm_client():
    return create_llm_client(os.environ["OPENAI_API_KEY"])


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def retriever(db_pool, llm_client) -> ContextRetriever:
    return ContextRetriever(db_pool, llm_client)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded_repo(db_pool, retriever, fixture_repo):
    """Seeds one real (embedded) code_chunks row for the fixture repo, and
    deletes it afterward — never leaves rows behind in the shared Tiger
    Cloud service."""
    embedding = await retriever.embed(EXISTING_DB_MODULE)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO code_chunks (repo, path, chunk_index, content, embedding)
            VALUES ($1, $2, 0, $3, $4)
            """,
            fixture_repo,
            VULNERABLE_FILE,
            EXISTING_DB_MODULE,
            embedding,
        )
    try:
        yield fixture_repo
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM code_chunks WHERE repo = $1", fixture_repo)


def _is_about_sql_injection(finding: Finding) -> bool:
    haystack = f"{finding.category} {finding.title} {finding.rationale}".lower()
    return "sql" in haystack and "inject" in haystack


async def test_seeded_repo_diff_produces_valid_findings(retriever, llm_client, seeded_repo):
    diff = DiffContext(repo_full_name=seeded_repo, pr_number=1, diff_text=VULNERABLE_DIFF)

    security, quality, tests, docs = (
        SecurityAgent(llm_client, retriever),
        QualityAgent(llm_client, retriever),
        TestsAgent(llm_client, retriever),
        DocsAgent(llm_client, retriever),
    )

    security_findings = await security.review(diff)
    quality_findings = await quality.review(diff)
    tests_findings = await tests.review(diff)
    docs_findings = await docs.review(diff)

    all_findings = security_findings + quality_findings + tests_findings + docs_findings

    # Every agent found at least one real thing to say about this diff.
    assert security_findings, "security agent returned no findings for a diff with an unparameterized query"
    assert quality_findings, "quality agent returned no findings"
    assert tests_findings, "tests agent returned no findings for untested new logic"
    assert docs_findings, "docs agent returned no findings for an undocumented new function"

    # Every finding is a real, schema-valid Finding (guaranteed by construction
    # via Finding.from_draft, but assert the actual type as the real check).
    assert all(isinstance(f, Finding) for f in all_findings)

    # The security agent specifically caught the seeded SQL injection.
    injection_findings = [f for f in security_findings if _is_about_sql_injection(f)]
    assert injection_findings, (
        f"no security finding referenced SQL injection; got: "
        f"{[(f.category, f.title) for f in security_findings]}"
    )
    assert any(f.file == VULNERABLE_FILE for f in injection_findings)
