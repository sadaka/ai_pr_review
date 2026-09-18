"""M2 demo: pytest backend/tests/test_webhook_ingress.py -k
"valid_signature and idempotent_replay and enqueues_job" """
from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from api.schemas import ReviewJob
from job_queue.queue import CircuitOpenError, JobEnqueueError
from tests.conftest import TEST_SECRET, pull_request_payload, sign
from webhook_receiver.app import create_app


class FakeJobQueue:
    """In-memory double for JobQueue — exercises webhook_receiver's contract with
    the queue without touching real Redis/ARQ."""

    def __init__(self, *, raise_circuit_open: bool = False, fail_enqueue_times: int = 0) -> None:
        self.seen: set[str] = set()
        self.enqueued: list[ReviewJob] = []
        self.raise_circuit_open = raise_circuit_open
        self.fail_enqueue_times = fail_enqueue_times  # simulates enqueue failing N times before succeeding

    async def mark_seen(self, delivery_id: str) -> bool:
        if self.raise_circuit_open:
            raise CircuitOpenError("simulated redis outage")
        if delivery_id in self.seen:
            return False
        self.seen.add(delivery_id)
        return True

    async def unclaim(self, delivery_id: str) -> None:
        self.seen.discard(delivery_id)

    async def enqueue_review(self, job: ReviewJob) -> str:
        if self.fail_enqueue_times > 0:
            self.fail_enqueue_times -= 1
            raise JobEnqueueError("simulated transient enqueue failure")
        self.enqueued.append(job)
        return "fake-job-id"


async def _post(app, body: bytes, *, signature: str | None, delivery: str | None, event: str = "pull_request"):
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    if event is not None:
        headers["X-GitHub-Event"] = event

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhook", content=body, headers=headers)


async def test_valid_signature_enqueues_job():
    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = pull_request_payload()

    resp = await _post(app, body, signature=sign(body), delivery="delivery-1")

    assert resp.status_code == 200
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0].delivery_id == "delivery-1"
    assert queue.enqueued[0].repo_full_name == "octocat/hello-world"
    assert queue.enqueued[0].pr_number == 42


async def test_idempotent_replay_does_not_enqueue_twice():
    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = pull_request_payload()
    sig = sign(body)

    first = await _post(app, body, signature=sig, delivery="delivery-2")
    second = await _post(app, body, signature=sig, delivery="delivery-2")  # GitHub redelivery

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(queue.enqueued) == 1  # only the first delivery actually enqueued


async def test_valid_signature_and_idempotent_replay_and_enqueues_job():
    """Single test matching the M2 demo command's -k filter verbatim: exercises
    the whole ingress contract end to end (valid signature -> enqueues once,
    replay -> idempotent, no second enqueue)."""
    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = pull_request_payload()
    sig = sign(body)

    first = await _post(app, body, signature=sig, delivery="delivery-e2e")
    replay = await _post(app, body, signature=sig, delivery="delivery-e2e")

    assert first.status_code == 200
    assert replay.status_code == 200
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0].delivery_id == "delivery-e2e"


async def test_bad_signature_rejected_nothing_enqueued():
    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = pull_request_payload()

    resp = await _post(app, body, signature="sha256=" + "0" * 64, delivery="delivery-3")

    assert resp.status_code == 401
    assert queue.enqueued == []


async def test_missing_signature_rejected():
    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = pull_request_payload()

    resp = await _post(app, body, signature=None, delivery="delivery-4")

    assert resp.status_code == 401
    assert queue.enqueued == []


async def test_non_pull_request_event_ignored_without_enqueue():
    queue = FakeJobQueue()
    app = create_app(queue, TEST_SECRET)
    body = pull_request_payload()

    resp = await _post(app, body, signature=sign(body), delivery="delivery-5", event="issue_comment")

    assert resp.status_code == 200
    assert queue.enqueued == []


async def test_enqueue_failure_rolls_back_dedup_claim_so_redelivery_is_not_lost():
    """Regression: if mark_seen succeeds but enqueue_review then fails, the delivery
    must NOT be permanently swallowed. A GitHub redelivery of the same id has to get
    a real second attempt, which must succeed and actually enqueue the job."""
    queue = FakeJobQueue(fail_enqueue_times=1)
    app = create_app(queue, TEST_SECRET)
    body = pull_request_payload()
    sig = sign(body)

    first = await _post(app, body, signature=sig, delivery="delivery-flaky")
    assert first.status_code == 503
    assert queue.enqueued == []
    assert "delivery-flaky" not in queue.seen  # claim was rolled back

    redelivery = await _post(app, body, signature=sig, delivery="delivery-flaky")
    assert redelivery.status_code == 200
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0].delivery_id == "delivery-flaky"


async def test_queue_unavailable_returns_503():
    queue = FakeJobQueue(raise_circuit_open=True)
    app = create_app(queue, TEST_SECRET)
    body = pull_request_payload()

    resp = await _post(app, body, signature=sign(body), delivery="delivery-6")

    assert resp.status_code == 503


class FakeRepoStatusStore:
    def __init__(self) -> None:
        self.marked_pending: list[str] = []

    async def mark_pending(self, repo_full_name: str) -> None:
        self.marked_pending.append(repo_full_name)


async def test_pull_request_event_does_not_touch_repo_status_store():
    queue = FakeJobQueue()
    status_store = FakeRepoStatusStore()
    app = create_app(queue, TEST_SECRET, status_store)
    body = pull_request_payload()

    resp = await _post(app, body, signature=sign(body), delivery="delivery-7")

    assert resp.status_code == 200
    assert status_store.marked_pending == []


