---
title: LangGraph Orchestrator
filed: 2026-09-04
milestone: M3
---

# LangGraph Orchestrator

**What it is:** `backend/core/workflow_engine.py` (the `WorkflowEngine` Protocol,
ADR-001) + `backend/orchestrator/` (the LangGraph implementation). The review
graph (`orchestrator/graph.py`) fans out to 4 stub specialist nodes
(`security`, `quality`, `tests`, `docs`) via the `Send` API, joins at a stub
`aggregate` node. M4 replaces the stub node bodies with real grounded LLM
calls — the graph shape and checkpoint/resume mechanics built here don't
change.

## The resume-input bug (don't regress this)

`LangGraphEngine.run` must pass **`None`**, not `{}`, as the graph input when
resuming an existing `thread_id`. LangGraph treats *any* non-`None` input —
including an empty dict — as a fresh invocation and re-dispatches from START,
re-running every node, even ones that already completed. `None` is the actual
"continue from the checkpoint" signal. This was M3's real L4-caught bug; see
[[Idempotent-Retry-Ordering]] for the related but distinct dedup pattern used
in the webhook layer.

## Why a hand-written Redis checkpointer

The official `langgraph-checkpoint-redis` package needs a RediSearch module
(via `redisvl`) that Upstash's free tier doesn't provide. `orchestrator/
redis_checkpointer.py` implements only the async `BaseCheckpointSaver` methods
actually exercised (`aget_tuple`, `aput`, `aput_writes`, `alist`,
`adelete_thread`) against plain Redis commands (get/set/hash/list). It stores
each checkpoint's `channel_values` fully inlined rather than using the
official saver's blob/version-delta scheme — simpler, and sufficient while
state stays small (stub result dicts); revisit once M4 puts real LLM findings
(larger payloads) into state.

## What "checkpoint at each node boundary" actually buys you

LangGraph calls `aput_writes` as soon as an individual parallel branch's node
coroutine *returns* — not batched at the end of the whole superstep. So if 2
of 4 specialists finish before a 3rd raises and aborts the run, those 2
completed nodes' writes are already durably recorded. On resume, LangGraph
reloads the checkpoint, sees which tasks already have a recorded write, and
only re-schedules the ones that don't — this is what makes "kill the worker
after 2/4 nodes, resume, don't re-run the completed 2" actually work, not a
custom mechanism this project built.

**Known gap:** a node that was *mid-flight* (started, not yet returned) when
the crash happens is indistinguishable from a node that never started —
`aput_writes` only fires on successful return, so both get re-run from
scratch on resume. Not a bug, just worth knowing when reasoning about "what
counts as complete."

See also: [[Webhook-Ingress-and-Job-Queue]]
