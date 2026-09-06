---
title: Idempotent Retry Ordering
filed: 2026-09-04
milestone: M2
---

# Idempotent Retry Ordering

**The pattern:** when a handler must both (a) record "this request was
processed" and (b) perform a side effect exactly once, claim-before-act beats
act-before-claim for retry safety — with a rollback to cover the failure mode
it introduces.

**Why claim first:** if the side effect (enqueue, write, external call) runs
*before* the dedup claim is recorded, a crash or failure in the gap between
the two lets a retried request slip past the (not-yet-written) dedup check and
perform the side effect a second time — a silent duplicate. Claiming first
closes that gap: a retry can never see "not yet processed" once the original
attempt got as far as claiming.

**The cost, and its fix:** claiming first means if the side effect *then*
fails, the claim is already sitting there — a legitimate retry would see
"already processed" and be dropped, never getting its side effect performed at
all. The fix is a rollback: on side-effect failure, release the claim so a
real retry gets a fair second attempt.

**Applied in:** [[Webhook-Ingress-and-Job-Queue]] — `mark_seen` (claim) before
`enqueue_review` (side effect), with `unclaim` as the rollback on enqueue
failure. Relevant again wherever M3+ introduces another "record once, act
once" boundary (e.g. orchestrator checkpoint writes, HITL queue inserts).
