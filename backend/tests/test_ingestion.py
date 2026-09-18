"""M13 demo: pytest backend/tests/test_ingestion.py -k
"full_index_populates_code_chunks and incremental_reindex_only_touches_changed_files
and removed_file_chunks_are_deleted and push_event_enqueues_reindex_job"

`test_push_event_enqueues_reindex_job` (and the chunker/source_fetcher unit
tests) are fully fake-driven — no credentials needed. The three
`code_chunks`-touching tests are genuinely live (real Tiger Cloud + OpenAI
embeddings, fake GitHub tarball fetch) and skip cleanly without
TIGER_DATABASE_URL/OPENAI_API_KEY, same convention as `test_specialists_e2e.py`.
"""
from __future__ import annotations

import io
import os
import tarfile
import uuid

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from api.schemas import IndexRepoJob, ReindexRepoJob, ReviewJob
from ingestion.chunker import chunk_file, chunk_repo
from ingestion.indexer import ChangedFiles, IndexResult, Indexer
from ingestion.source_fetcher import FetchedSource, fetch_tarball
from job_queue.arq_worker import index_repo, reindex_repo
from tests.conftest import TEST_SECRET, sign
from webhook_receiver.app import create_app

pytestmark_live = pytest.mark.skipif(
    not (os.environ.get("TIGER_DATABASE_URL") and os.environ.get("OPENAI_API_KEY", "").startswith("sk-")),
    reason="requires live TIGER_DATABASE_URL and OPENAI_API_KEY",
)


# ── fakes ─────────────────────────────────────────────────────────────────


def _make_tarball(files: dict[str, str], *, top_dir: str = "acme-widgets-abc1234") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel_path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"{top_dir}/{rel_path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeGitHub:
    """Fake `SupportsTarballFetch` — no real GitHub App auth needed. Each call
    returns whatever tarball is currently registered for `ref`."""

    def __init__(self, tarballs_by_ref: dict[str, bytes], *, head_sha: str = "sha-head") -> None:
        self._tarballs = tarballs_by_ref
        self._head_sha = head_sha
        self.download_calls: list[str] = []

    async def get_repo_head_sha(self, *, repo_full_name: str, ref: str = "HEAD") -> str:
        return self._head_sha if ref == "HEAD" else ref

    async def download_tarball(self, *, repo_full_name: str, ref: str) -> bytes:
        self.download_calls.append(ref)
        return self._tarballs[ref]


class FakeJobQueue:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.enqueued_index: list[IndexRepoJob] = []
        self.enqueued_reindex: list[ReindexRepoJob] = []

    async def mark_seen(self, delivery_id: str) -> bool:
        if delivery_id in self.seen:
            return False
        self.seen.add(delivery_id)
        return True

    async def unclaim(self, delivery_id: str) -> None:
        self.seen.discard(delivery_id)

    async def enqueue_review(self, job: ReviewJob) -> str:
        raise AssertionError("not exercised by these tests")

    async def enqueue_index_repo(self, job: IndexRepoJob) -> str:
        self.enqueued_index.append(job)
        return "fake-index-job-id"

    async def enqueue_reindex_repo(self, job: ReindexRepoJob) -> str:
        self.enqueued_reindex.append(job)
        return "fake-reindex-job-id"


class FakeRepoStatusStore:
    def __init__(self) -> None:
        self.marked_pending: list[str] = []

    async def mark_pending(self, repo_full_name: str) -> None:
        self.marked_pending.append(repo_full_name)


# ── chunker unit tests ───────────────────────────────────────────────────


def test_chunk_file_splits_python_by_top_level_symbol():
    text = (
        "import os\n\n"
        "def foo():\n    return 1\n\n\n"
        "class Bar:\n    def method(self):\n        return 2\n"
    )
    chunks = chunk_file(__import__("pathlib").Path("app/mod.py"), text)

    symbols = [c.symbol for c in chunks]
    assert symbols == [None, "foo", "Bar"]
    assert all(c.path == "app/mod.py" for c in chunks)
    assert [c.chunk_index for c in chunks] == [0, 1, 2]


def test_chunk_repo_skips_vendored_lockfile_and_binary(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def run():\n    pass\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("module.exports = 1;")
    (tmp_path / "uv.lock").write_text("# lockfile")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    chunks = chunk_repo(tmp_path)

    assert {c.path for c in chunks} == {"src/main.py"}


def test_chunk_repo_only_paths_filters_to_the_given_set(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    pass\n")
    (tmp_path / "b.py").write_text("def b():\n    pass\n")

    chunks = chunk_repo(tmp_path, only_paths={"a.py"})

    assert {c.path for c in chunks} == {"a.py"}


# ── source_fetcher unit tests ────────────────────────────────────────────


async def test_fetch_tarball_extracts_and_resolves_sha():
    tarball = _make_tarball({"README.md": "hello", "src/app.py": "def f():\n    pass\n"})
    github = FakeGitHub({"sha-head": tarball}, head_sha="sha-head")

    fetched = await fetch_tarball(github, repo_full_name="acme/widgets")

    assert isinstance(fetched, FetchedSource)
    assert fetched.commit_sha == "sha-head"
    assert (fetched.root / "README.md").read_text() == "hello"
    assert github.download_calls == ["sha-head"]


async def test_fetch_tarball_rejects_path_traversal():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"evil"
        info = tarfile.TarInfo(name="acme-widgets-abc1234/../../evil.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    github = FakeGitHub({"sha-head": buf.getvalue()}, head_sha="sha-head")

    with pytest.raises(ValueError, match="escapes extraction root"):
        await fetch_tarball(github, repo_full_name="acme/widgets")


# ── webhook routing (fake-driven, no creds) ──────────────────────────────


async def _post(app, body: bytes, *, event: str, delivery: str = "delivery-1"):
    headers = {
        "content-type": "application/json",
        "X-Hub-Signature-256": sign(body),
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": event,
    }
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhook", content=body, headers=headers)


def _push_payload(*, added=(), modified=(), removed=(), repo="acme/widgets", before="sha-before", after="sha-after"):
    import json

    return json.dumps(
        {
            "before": before,
            "after": after,
            "repository": {"full_name": repo},
            "commits": [{"added": list(added), "modified": list(modified), "removed": list(removed)}],
        }
    ).encode()


async def test_push_event_enqueues_reindex_job():
    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = _push_payload(added=["new.py"], modified=["existing.py"], removed=["gone.py"])

    resp = await _post(app, body, event="push")

    assert resp.status_code == 200
    assert len(queue.enqueued_reindex) == 1
    job = queue.enqueued_reindex[0]
    assert job.repo_full_name == "acme/widgets"
    assert job.before_sha == "sha-before" and job.after_sha == "sha-after"
    assert job.added == ["new.py"] and job.modified == ["existing.py"] and job.removed == ["gone.py"]


async def test_push_event_marks_repo_pending():
    queue = FakeJobQueue()
    status_store = FakeRepoStatusStore()
    app = create_app(queue, TEST_SECRET, status_store)
    body = _push_payload(added=["new.py"])

    resp = await _post(app, body, event="push")

    assert resp.status_code == 200
    assert status_store.marked_pending == ["acme/widgets"]


async def test_push_event_with_no_file_changes_enqueues_nothing():
    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = _push_payload()

    resp = await _post(app, body, event="push")

    assert resp.status_code == 200
    assert queue.enqueued_reindex == []


async def test_installation_created_event_enqueues_index_repo_per_repo():
    import json

    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = json.dumps(
        {
            "action": "created",
            "repositories": [{"full_name": "acme/widgets"}, {"full_name": "acme/gadgets"}],
        }
    ).encode()

    resp = await _post(app, body, event="installation")

    assert resp.status_code == 200
    assert {j.repo_full_name for j in queue.enqueued_index} == {"acme/widgets", "acme/gadgets"}


async def test_installation_created_event_marks_each_repo_pending():
    import json

    queue = FakeJobQueue()
    status_store = FakeRepoStatusStore()
    app = create_app(queue, TEST_SECRET, status_store)
    body = json.dumps(
        {
            "action": "created",
            "repositories": [{"full_name": "acme/widgets"}, {"full_name": "acme/gadgets"}],
        }
    ).encode()

    resp = await _post(app, body, event="installation")

    assert resp.status_code == 200
    assert set(status_store.marked_pending) == {"acme/widgets", "acme/gadgets"}


# ── arq worker: index_repo/reindex_repo mark the repo failed on error ────


class FakeIndexer:
    """Fake `Indexer` — exercises the ARQ job functions' failure-handling
    wiring (`mark_failed` called, then the original exception re-raised so
    ARQ's own retry/max_tries machinery still sees it) without touching a
    real DB, GitHub, or OpenAI."""

    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self._raise_error = raise_error
        self.failed: list[tuple[str, str]] = []

    async def full_index(self, repo_full_name: str) -> IndexResult:
        if self._raise_error:
            raise self._raise_error
        return IndexResult(repo_full_name=repo_full_name, commit_sha="sha-1", chunk_count=1, files_embedded=1)

    async def incremental_index(self, repo_full_name: str, **kwargs: object) -> IndexResult:
        if self._raise_error:
            raise self._raise_error
        return IndexResult(repo_full_name=repo_full_name, commit_sha="sha-2", chunk_count=1, files_embedded=1)

    async def mark_failed(self, repo_full_name: str, error: str) -> None:
        self.failed.append((repo_full_name, error))


async def test_index_repo_marks_failed_and_reraises_on_error():
    indexer = FakeIndexer(raise_error=RuntimeError("tarball fetch failed"))
    job = IndexRepoJob(delivery_id="d1", repo_full_name="acme/widgets").model_dump()

    with pytest.raises(RuntimeError, match="tarball fetch failed"):
        await index_repo({"indexer": indexer}, job)

    assert indexer.failed == [("acme/widgets", "tarball fetch failed")]


async def test_index_repo_does_not_mark_failed_on_success():
    indexer = FakeIndexer()
    job = IndexRepoJob(delivery_id="d1", repo_full_name="acme/widgets").model_dump()

    result = await index_repo({"indexer": indexer}, job)

    assert result["repo_full_name"] == "acme/widgets"
    assert indexer.failed == []


async def test_reindex_repo_marks_failed_and_reraises_on_error():
    indexer = FakeIndexer(raise_error=RuntimeError("embedding call failed"))
    job = ReindexRepoJob(
        delivery_id="d1",
        repo_full_name="acme/widgets",
        before_sha="sha-before",
        after_sha="sha-after",
        added=["new.py"],
        modified=[],
        removed=[],
    ).model_dump()

    with pytest.raises(RuntimeError, match="embedding call failed"):
        await reindex_repo({"indexer": indexer}, job)

    assert indexer.failed == [("acme/widgets", "embedding call failed")]


# ── live: full index / incremental / removed-file deletion ──────────────
#
# Scoped to a class (not a module-level `pytestmark`) so the skipif/loop_scope
# marks apply only to these live tests, not the fake-driven ones above.


@pytest.fixture(scope="module")
def fixture_repo() -> str:
    return f"test-fixture/m13-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db_pool():
    from memory.context_retriever import create_pool

    pool = await create_pool(os.environ["TIGER_DATABASE_URL"])
    yield pool
    await pool.close()


@pytest.fixture(scope="module")
def llm_client():
    from agents.llm_client import create_llm_client

    return create_llm_client(os.environ["OPENAI_API_KEY"])


@pytest_asyncio.fixture(loop_scope="module")
async def clean_repo(db_pool, fixture_repo):
    try:
        yield fixture_repo
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM code_chunks WHERE repo = $1", fixture_repo)
            await conn.execute("DELETE FROM repo_index_state WHERE repo = $1", fixture_repo)


@pytestmark_live
@pytest.mark.asyncio(loop_scope="module")
class TestLiveIndexing:
    async def test_full_index_populates_code_chunks(self, db_pool, llm_client, clean_repo):
        repo = clean_repo
        tarball = _make_tarball(
            {
                "src/a.py": "def a():\n    return 1\n",
                "src/b.py": "def b():\n    return 2\n",
                "README.md": "docs " * 10,
            }
        )
        github = FakeGitHub({"sha-1": tarball}, head_sha="sha-1")
        indexer = Indexer(db_pool, llm_client, github)

        result = await indexer.full_index(repo)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT path, chunk_index FROM code_chunks WHERE repo = $1", repo)
            state = await conn.fetchrow(
                "SELECT last_indexed_commit, chunk_count FROM repo_index_state WHERE repo = $1", repo
            )

        assert result.commit_sha == "sha-1"
        assert result.files_embedded == 3
        assert len(rows) == result.chunk_count
        assert {r["path"] for r in rows} == {"src/a.py", "src/b.py", "README.md"}
        assert state["last_indexed_commit"] == "sha-1"
        assert state["chunk_count"] == len(rows)

    async def test_incremental_reindex_only_touches_changed_files(
        self, db_pool, llm_client, clean_repo, monkeypatch
    ):
        repo = clean_repo
        initial_tarball = _make_tarball(
            {
                "f1.py": "def f1():\n    return 1\n",
                "f2.py": "def f2():\n    return 2\n",
                "f3.py": "def f3():\n    return 3\n",
                "f4.py": "def f4():\n    return 4\n",
                "f5.py": "def f5():\n    return 5\n",
            }
        )
        github = FakeGitHub({"sha-1": initial_tarball}, head_sha="sha-1")
        indexer = Indexer(db_pool, llm_client, github)
        await indexer.full_index(repo)

        async with db_pool.acquire() as conn:
            before = {
                r["path"]: r["updated_at"]
                for r in await conn.fetch("SELECT path, updated_at FROM code_chunks WHERE repo = $1", repo)
            }

        updated_tarball = _make_tarball(
            {
                "f1.py": "def f1():\n    return 100\n",  # modified
                "f2.py": "def f2():\n    return 2\n",
                "f3.py": "def f3():\n    return 3\n",
                "f4.py": "def f4():\n    return 4\n",
                "f6.py": "def f6():\n    return 6\n",  # added
            }
        )
        github._tarballs["sha-2"] = updated_tarball

        embed_calls = 0
        real_embed = indexer.embed

        async def _counting_embed(text: str):
            nonlocal embed_calls
            embed_calls += 1
            return await real_embed(text)

        monkeypatch.setattr(indexer, "embed", _counting_embed)

        result = await indexer.incremental_index(
            repo,
            before_sha="sha-1",
            after_sha="sha-2",
            changed=ChangedFiles(added=["f6.py"], modified=["f1.py"], removed=["f5.py"]),
        )

        assert embed_calls == 2  # exactly f1.py + f6.py re-embedded, not f2/f3/f4
        assert result.files_embedded == 2

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT path, updated_at FROM code_chunks WHERE repo = $1", repo)
        after = {r["path"]: r["updated_at"] for r in rows}

        assert "f5.py" not in after  # removed
        assert "f6.py" in after  # added
        assert after["f1.py"] > before["f1.py"]  # modified — re-written
        for untouched in ("f2.py", "f3.py", "f4.py"):
            assert after[untouched] == before[untouched]  # never re-written

    async def test_removed_file_chunks_are_deleted(self, db_pool, llm_client, clean_repo):
        repo = clean_repo
        tarball = _make_tarball({"keep.py": "def keep():\n    pass\n", "gone.py": "def gone():\n    pass\n"})
        github = FakeGitHub({"sha-1": tarball}, head_sha="sha-1")
        indexer = Indexer(db_pool, llm_client, github)
        await indexer.full_index(repo)

        await indexer.incremental_index(
            repo, before_sha="sha-1", after_sha="sha-2", changed=ChangedFiles(removed=["gone.py"])
        )

        async with db_pool.acquire() as conn:
            remaining = {
                r["path"] for r in await conn.fetch("SELECT path FROM code_chunks WHERE repo = $1", repo)
            }

        assert remaining == {"keep.py"}

    async def test_full_index_populates_code_chunks_and_incremental_reindex_only_touches_changed_files_and_removed_file_chunks_are_deleted_and_push_event_enqueues_reindex_job(  # noqa: E501
        self, db_pool, llm_client, clean_repo, monkeypatch
    ):
        """Single test matching the M13 demo command's `-k` filter verbatim
        (same convention as `test_webhook_ingress.py`'s combined test): exercises
        the whole ingestion contract end to end in one run — full index, then an
        incremental reindex that only re-embeds changed files and deletes removed
        ones, then confirms a `push` webhook routes to exactly that reindex job."""
        repo = clean_repo

        # 1. full_index_populates_code_chunks
        initial_tarball = _make_tarball(
            {
                "f1.py": "def f1():\n    return 1\n",
                "f2.py": "def f2():\n    return 2\n",
                "gone.py": "def gone():\n    pass\n",
            }
        )
        github = FakeGitHub({"sha-1": initial_tarball}, head_sha="sha-1")
        indexer = Indexer(db_pool, llm_client, github)
        full_result = await indexer.full_index(repo)
        assert full_result.files_embedded == 3

        async with db_pool.acquire() as conn:
            populated = {r["path"] for r in await conn.fetch("SELECT path FROM code_chunks WHERE repo = $1", repo)}
        assert populated == {"f1.py", "f2.py", "gone.py"}

        # 2. incremental_reindex_only_touches_changed_files (+ removed_file_chunks_are_deleted)
        updated_tarball = _make_tarball(
            {
                "f1.py": "def f1():\n    return 100\n",  # modified
                "f2.py": "def f2():\n    return 2\n",  # untouched
                "f3.py": "def f3():\n    return 3\n",  # added
            }
        )
        github._tarballs["sha-2"] = updated_tarball

        embed_calls = 0
        real_embed = indexer.embed

        async def _counting_embed(text: str):
            nonlocal embed_calls
            embed_calls += 1
            return await real_embed(text)

        monkeypatch.setattr(indexer, "embed", _counting_embed)

        incremental_result = await indexer.incremental_index(
            repo,
            before_sha="sha-1",
            after_sha="sha-2",
            changed=ChangedFiles(added=["f3.py"], modified=["f1.py"], removed=["gone.py"]),
        )

        assert embed_calls == 2  # only f1.py (modified) + f3.py (added)
        assert incremental_result.files_embedded == 2

        async with db_pool.acquire() as conn:
            remaining = {r["path"] for r in await conn.fetch("SELECT path FROM code_chunks WHERE repo = $1", repo)}
        assert remaining == {"f1.py", "f2.py", "f3.py"}  # gone.py deleted, f2.py untouched but still present

        # 3. push_event_enqueues_reindex_job
        queue = FakeJobQueue()
        app = create_app(queue, TEST_SECRET)
        push_body = _push_payload(
            repo=repo, before="sha-1", after="sha-2", added=["f3.py"], modified=["f1.py"], removed=["gone.py"]
        )

        resp = await _post(app, push_body, event="push", delivery=f"delivery-{repo}")

        assert resp.status_code == 200
        assert len(queue.enqueued_reindex) == 1
        job = queue.enqueued_reindex[0]
        assert job.repo_full_name == repo
        assert job.added == ["f3.py"] and job.modified == ["f1.py"] and job.removed == ["gone.py"]
