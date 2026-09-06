"""The workflow-engine interface — ADR-001 (deferred framework commitment).

Everything above this line (job_queue, and eventually the aggregator/HITL/API
layers) depends on `WorkflowEngine`, never on LangGraph directly. That's the
whole point: LangGraph is an implementation detail behind this interface, not
a dependency that leaks outward. If a future milestone needs to switch to
Temporal, or drop down to a hand-rolled state machine for one workflow, only
`orchestrator/**` changes — nothing that calls `WorkflowEngine` does.

Per `backend/core/**`'s dependency-direction invariant: this module imports
NOTHING else under `backend/` — it is the innermost layer.
"""
from __future__ import annotations

from typing import Any, Protocol


class WorkflowEngine(Protocol):
    """A workflow engine runs a named graph of work to completion, checkpointing
    as it goes so a run can be resumed (not restarted from scratch) after a
    crash. `thread_id` is the resumption key: calling `run` again with the same
    `thread_id` continues an in-flight run rather than starting a new one."""

    async def run(self, initial_state: dict[str, Any], *, thread_id: str) -> dict[str, Any]:
        """Run (or resume) the workflow for `thread_id` to completion and return
        the final state. Raises whatever exception aborted the run, if any —
        callers decide whether/how to retry by calling `run` again with the
        same `thread_id`."""
        ...
