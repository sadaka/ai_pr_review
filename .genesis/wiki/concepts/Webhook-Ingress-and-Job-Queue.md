---
title: Webhook Ingress and Job Queue
filed: 2026-09-04
milestone: M2
---

# Webhook Ingress and Job Queue

**What it is:** `backend/webhook_receiver/` (FastAPI `/webhook` endpoint) +
`backend/job_queue/` (Redis-backed `JobQueue` wrapping ARQ). Verifies GitHub's
HMAC-SHA256 signature, dedups on `X-GitHub-Delivery`, enqueues a `ReviewJob`
(`backend/api/schemas.py`) to ARQ, and returns before any review work happens.

## The claim-then-confirm dedup pattern

`JobQueue.mark_seen(delivery_id)` does a Redis `SET NX EX 24h` — the FIRST call
for a given delivery id returns `True` and "claims" it; any replay returns
`False`. The webhook handler calls this **before** `enqueue_review`, not after
— see [[Idempotent-Retry-Ordering]] for why. If the subsequent enqueue then
fails, the handler calls `JobQueue.unclaim(delivery_id)` to release the claim,
so a genuine GitHub redelivery of the same id isn't silently dropped.

## Outbound-call safety (context-graph invariant)

Every Redis call goes through `JobQueue._guarded`: a `CircuitBreaker`
(opens after 3 consecutive failures, 30s cooldown, then a single half-open
probe) wrapping `_with_retry` (3 attempts, exponential backoff starting at
0.1s). The Redis client itself is built directly as `ArqRedis(...)` rather
than via `arq.connections.create_pool`, because `create_pool` only wires a
TCP connect timeout (`socket_connect_timeout`) — it never sets
`socket_timeout` (the per-command timeout), which would let a stalled command
after connect hang the webhook request indefinitely.

## Testing approach

`tests/test_webhook_ingress.py` uses an in-memory `FakeJobQueue` double to
test the HTTP-layer contract without touching real Redis/ARQ.
`tests/test_job_queue.py` uses `fakeredis` (for real dedup semantics) and hand-
written failing doubles (for retry/circuit-breaker behavior) — real Redis was
never exercised live in-session; a manual smoke test against the actual
Upstash instance is still recommended before relying on this in production.

## Known gap (not fixed, explicitly out of scope per M2's L4 VERIFY)

Two concurrent redeliveries of the *same* delivery id could theoretically race
across the `unclaim` → `mark_seen` boundary and both end up enqueuing (the
guarantee that's actually required — no double-enqueue in the sequential/
single-flight redelivery case — holds). Revisit if GitHub's redelivery
behavior is ever observed to fire concurrently rather than sequentially.

See also: [[Idempotent-Retry-Ordering]]
