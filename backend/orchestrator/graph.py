"""M3 scope: the StateGraph shape only — parallel fan-out to 4 STUB specialist
nodes + a stub aggregator join. No LLM calls, no retrieval, no real findings —
that's M4 (`agents/**`). This milestone proves the orchestration mechanics
(parallel execution + checkpoint/resume) work, independent of what the nodes
eventually do.
"""
from __future__ import annotations

import asyncio
import operator
import time
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

SPECIALIST_NAMES: tuple[str, ...] = ("security", "quality", "tests", "docs")


class ReviewState(TypedDict):
    pr_number: int
    repo_full_name: str
    # each specialist appends exactly one result dict; `operator.add` is the
    # reducer LangGraph uses to merge concurrent parallel writes to this
    # channel instead of one clobbering another.
    specialist_results: Annotated[list[dict[str, Any]], operator.add]
    aggregated: dict[str, Any] | None


def _dispatch_to_specialists(state: ReviewState) -> list[Send]:
    """Fan-out via the Send API: one Send per specialist, each carrying its
    own name so the shared node body knows which specialist it's standing in
    for."""
    return [Send(name, {**state, "specialist_name": name}) for name in SPECIALIST_NAMES]


def _make_specialist_node(name: str):
    """Stub specialist: no LLM/grounding yet (M4), just proves this branch of
    the fan-out actually ran, with a call-hook so tests can observe/interrupt
    it without polluting the persisted graph state."""

    async def specialist_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        hooks = (config.get("configurable") or {}).get("test_hooks") or {}
        if on_start := hooks.get("on_start"):
            on_start(name, time.monotonic())
        if should_crash := hooks.get("should_crash"):
            if should_crash(name):
                raise RuntimeError(f"simulated crash in specialist node '{name}'")

        # Yields control so concurrent branches actually interleave; tests set a
        # non-zero delay to make overlapping execution observable.
        await asyncio.sleep(hooks.get("delay_seconds", 0))

        if on_complete := hooks.get("on_complete"):
            on_complete(name, time.monotonic())
        return {
            "specialist_results": [
                {"agent": name, "status": "stub-complete", "completed_at": time.time()}
            ]
        }

    return specialist_node


async def _aggregate(state: ReviewState, config: RunnableConfig) -> dict[str, Any]:
    hooks = (config.get("configurable") or {}).get("test_hooks") or {}
    if on_run := hooks.get("on_run"):
        on_run("aggregate")

    results = state["specialist_results"]
    return {
        "aggregated": {
            "agent_count": len(results),
            "agents": sorted(r["agent"] for r in results),
        }
    }


def build_graph() -> StateGraph:
    """Build the (uncompiled) review StateGraph. Compilation (with a
    checkpointer) happens in `LangGraphEngine` — kept separate so tests can
    compile with whatever checkpointer they need."""
    graph = StateGraph(ReviewState)

    for name in SPECIALIST_NAMES:
        graph.add_node(name, _make_specialist_node(name))
        graph.add_edge(name, "aggregate")

    graph.add_node("aggregate", _aggregate)
    graph.add_conditional_edges(START, _dispatch_to_specialists, list(SPECIALIST_NAMES))
    graph.add_edge("aggregate", END)

    return graph
