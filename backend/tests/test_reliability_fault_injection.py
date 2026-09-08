"""M10: reliability under fault injection.

Every test here induces a failure — a transient error, a timeout, a dead
dependency, a retried write — and asserts the reliability layer behaves:
retries the right things, fails fast when the breaker is open, and never
duplicates a side effect on retry. Fake-driven, no credentials; one live
regression test for the finding-dedup index skips without TIGER_DATABASE_URL.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from reliability import CircuitBreaker, Guard, OperationTimeout, retry_async, with_timeout
from reliability.circuit_breaker import CircuitOpenError
from reliability.conformance import assert_breaker_contract
from reliability.idempotency import OnceGuard, fingerprint


class Boom(RuntimeError):
    pass


class _Counter:
    def __init__(self, fail_times: int, exc: BaseException | None = None) -> None:
        self.calls = 0
        self._fail_times = fail_times
        self._exc = exc or Boom("transient")

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return "ok"


# ── retry ─────────────────────────────────────────────────────────────────

async def test_retry_then_succeeds() -> None:
    op = _Counter(fail_times=2)
    result = await retry_async(op, attempts=3, base_delay=0)
    assert result == "ok"
    assert op.calls == 3  # proves it actually retried, not one-shot


async def test_retry_exhausted_reraises_last() -> None:
    op = _Counter(fail_times=99)
    with pytest.raises(Boom):
        await retry_async(op, attempts=3, base_delay=0)
    assert op.calls == 3


async def test_non_retryable_exception_not_retried() -> None:
    op = _Counter(fail_times=99, exc=ValueError("bad input"))
    with pytest.raises(ValueError):
        await retry_async(op, attempts=5, base_delay=0, retry_on=(KeyError,))
    assert op.calls == 1  # ValueError is not in retry_on → immediate


# ── timeout ───────────────────────────────────────────────────────────────

async def test_timeout_surfaces_as_operation_timeout() -> None:
    async def slow() -> None:
        await asyncio.sleep(10)

    with pytest.raises(OperationTimeout):
        await with_timeout(slow(), 0.01, name="slow")


async def test_fast_op_under_timeout_returns() -> None:
    async def quick() -> int:
        return 42

    assert await with_timeout(quick(), 1.0) == 42


# ── circuit breaker via Guard ─────────────────────────────────────────────

async def test_breaker_opens_and_fast_fails() -> None:
    breaker = CircuitBreaker(name="t", failure_threshold=2, reset_after_seconds=999)
    guard = Guard(breaker=breaker, attempts=1, base_delay=0)
    op = _Counter(fail_times=99)

    for _ in range(2):
        with pytest.raises(Boom):
            await guard(op)
    assert op.calls == 2

    # breaker now open: the op must NOT be invoked
    with pytest.raises(CircuitOpenError):
        await guard(op)
    assert op.calls == 2  # frozen — fast-failed without touching the dependency


async def test_breaker_half_opens_after_cooldown() -> None:
    breaker = CircuitBreaker(name="t", failure_threshold=1, reset_after_seconds=0)
    guard = Guard(breaker=breaker, attempts=1, base_delay=0)
    op = _Counter(fail_times=1)  # fail once (opens), then succeed on the probe

    with pytest.raises(Boom):
        await guard(op)
    # cooldown is 0 → next call is a probe that reaches the op
    assert await guard(op) == "ok"
    assert breaker.state == "closed"


async def test_cancellation_does_not_count_as_breaker_failure() -> None:
    breaker = CircuitBreaker(name="t", failure_threshold=1, reset_after_seconds=999)
    guard = Guard(breaker=breaker, attempts=1, base_delay=0)

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await guard(cancelled)
    breaker.before_call()  # still closed — a cancellation is not a dependency failure
    assert breaker.state == "closed"


async def test_non_consecutive_failures_do_not_open_breaker() -> None:
    breaker = CircuitBreaker(name="t", failure_threshold=2, reset_after_seconds=999)
    guard = Guard(breaker=breaker, attempts=1, base_delay=0)

    with pytest.raises(Boom):
        await guard(_Counter(fail_times=1))
    await guard(_Counter(fail_times=0))  # success resets the count
    with pytest.raises(Boom):
        await guard(_Counter(fail_times=1))

    breaker.before_call()  # still closed — no raise
    assert breaker.state == "closed"


# ── idempotency ───────────────────────────────────────────────────────────

class _FakePool:
    """Records finding rows; drops the connection on the first executemany."""

    def __init__(self, *, drop_first: bool = True) -> None:
        self.rows: list[tuple] = []
        self._drop_first = drop_first
        self.executemany_calls = 0

    async def executemany(self, _sql: str, args: list[tuple]) -> None:
        self.executemany_calls += 1
        if self._drop_first and self.executemany_calls == 1:
            raise ConnectionError("connection reset")
        # ON CONFLICT DO NOTHING semantics: only insert rows not already present
        for row in args:
            if row not in self.rows:
                self.rows.append(row)


async def test_retried_db_write_has_no_duplicate_effect() -> None:
    from reliability import Guard as _Guard

    pool = _FakePool(drop_first=True)
    guard = _Guard(breaker=CircuitBreaker(name="db"), attempts=3, base_delay=0,
                   retry_on=(ConnectionError,))

    findings = [("r1", "security", "app.py", 1), ("r1", "tests", "app.py", None)]

    async def _write() -> None:
        await pool.executemany("INSERT ... ON CONFLICT DO NOTHING", list(findings))

    # First call: retried (drop, then succeed). Second call: a full replay.
    await guard(_write)
    await guard(_write)

    assert pool.executemany_calls == 3  # 1 dropped + 1 retry-success + 1 replay
    assert sorted(pool.rows) == sorted(findings)  # exactly one copy of each, despite 3 attempts


def test_fingerprint_is_stable_and_order_sensitive() -> None:
    assert fingerprint("r1", "security", "app.py", 1) == fingerprint("r1", "security", "app.py", 1)
    assert fingerprint("r1", "security", "app.py", 1) != fingerprint("r1", "security", "app.py", 2)
    assert fingerprint("a", "b") != fingerprint("b", "a")


def test_once_guard_applies_side_effect_once_per_key() -> None:
    guard = OnceGuard()
    applied = 0
    for _ in range(5):
        if guard.first_time("deliver:42"):
            applied += 1
    assert applied == 1


# ── LLM breaker ───────────────────────────────────────────────────────────

class _FakeChat:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    @property
    def completions(self):  # noqa: ANN201
        return self

    async def parse(self, **_kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        raise self._exc


class _FakeLLM:
    def __init__(self, exc: BaseException) -> None:
        self.chat = _FakeChat(exc)


async def test_llm_breaker_trips_after_repeated_failures() -> None:
    # Rebuild the module-level guard with a low threshold so the test is fast.
    from agents import llm_client

    original = llm_client._chat_guard
    llm_client._chat_guard = Guard(
        breaker=CircuitBreaker(name="openai-test", failure_threshold=3),
        attempts=1,
        timeout_seconds=5,
    )
    try:
        llm = _FakeLLM(RuntimeError("500 server error"))
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await llm_client.guarded_parse(llm, model="x", messages=[])
        assert llm.chat.calls == 3
        # breaker open now → fast-fail, fake not called again
        with pytest.raises(CircuitOpenError):
            await llm_client.guarded_parse(llm, model="x", messages=[])
        assert llm.chat.calls == 3
    finally:
        llm_client._chat_guard = original


# ── conformance: all three breakers share one contract ────────────────────

def test_reliability_circuit_breaker_satisfies_contract() -> None:
    assert_breaker_contract(
        lambda threshold, reset: CircuitBreaker(
            name="c", failure_threshold=threshold, reset_after_seconds=reset
        )
    )


def test_job_queue_circuit_breaker_satisfies_contract() -> None:
    from job_queue.queue import CircuitBreaker as JQBreaker

    assert_breaker_contract(
        lambda threshold, reset: JQBreaker(failure_threshold=threshold, reset_after_seconds=reset)
    )


def test_github_client_circuit_breaker_satisfies_contract() -> None:
    from integrations.github_client import _CircuitBreaker as GHBreaker

    assert_breaker_contract(lambda threshold, reset: GHBreaker(threshold=threshold, reset_after=reset))


# ── live regression: the dedup index actually dedups ──────────────────────

_LIVE = bool(os.environ.get("TIGER_DATABASE_URL"))


@pytest.mark.skipif(not _LIVE, reason="requires live TIGER_DATABASE_URL")
@pytest.mark.asyncio(loop_scope="module")
async def test_finding_dedup_index_regression() -> None:
    import uuid

    from agents.contracts import AgentType, Finding, Severity
    from integrations.truth_store import TruthStore

    store = await TruthStore.connect(os.environ["TIGER_DATABASE_URL"])
    delivery = f"m10-dedup-{uuid.uuid4().hex[:12]}"
    try:
        review = await store.upsert_review(repo="test/m10", pr_number=1, delivery_id=delivery)
        findings = [
            Finding(agent_type=AgentType.SECURITY, severity=Severity.HIGH, category="sql-injection",
                    file="app/db.py", line=13, confidence=0.9, title="x", rationale="y"),
            Finding(agent_type=AgentType.TESTS, severity=Severity.MEDIUM, category="missing-test",
                    file="app/db.py", line=None, confidence=0.8, title="x", rationale="y"),
        ]
        await store.insert_findings(review_id=review.id, findings=findings)
        await store.insert_findings(review_id=review.id, findings=findings)  # replay

        count = await store._pool.fetchval(
            "SELECT count(*) FROM finding_records WHERE review_id = $1", review.id
        )
        assert count == 2, f"dedup index failed — got {count} rows for 2 distinct findings"
    finally:
        async with store._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM finding_records WHERE review_id IN "
                "(SELECT id FROM pr_review_records WHERE github_delivery_id = $1)",
                delivery,
            )
            await conn.execute("DELETE FROM pr_review_records WHERE github_delivery_id = $1", delivery)
        await store.close()
