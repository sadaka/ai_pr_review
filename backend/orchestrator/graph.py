"""The review StateGraph — two builders share one shape:

  - `build_graph()` — the M3 stub graph: 4 stub specialist nodes + a stub
    aggregator. No LLM, no retrieval, no real findings. Still used by
    `test_orchestrator.py` to prove the orchestration mechanics (parallel
    fan-out + checkpoint/resume) independent of what the nodes do.

  - `build_review_graph(deps)` — the M11 real graph: each specialist node runs
    the corresponding M4 agent (`deps.agents[name].review(...)`) and the
    aggregator node calls `orchestrator.nodes.aggregate` (the M5 dedup →
    confidence gate → GitHub post / HITL queue → truth write).

`dependency_direction`: this module imports `agents.specialists` (declared
edge) and `orchestrator.nodes` (same package). The aggregator's collaborators
(`github`, `truth_store`, `hitl`, `events`) are injected via the narrow
Protocols `nodes.py` already defines — so there is still no
`orchestrator → integrations` / `orchestrator → hitl` import.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agents.base_agent import DiffContext, SpecialistAgent
from agents.contracts import Finding
from observability.events import NullEventSink, SupportsEmit
from orchestrator import nodes
from orchestrator.nodes import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    SupportsHitlQueue,
    SupportsPostReview,
    SupportsTruthStore,
)
from orchestrator.state import ReviewState

SPECIALIST_NAMES: tuple[str, ...] = ("security", "quality", "tests", "docs")


# ── the real (M11) graph ───────────────────────────────────────────────────


@dataclass
class GraphDeps:
    """Everything the real nodes need, injected at build time (not carried as
    JSON state — these are live clients/pools)."""

    agents: Mapping[str, SpecialistAgent]
    github: SupportsPostReview
    truth_store: SupportsTruthStore
    hitl: SupportsHitlQueue
    events: SupportsEmit = field(default_factory=NullEventSink)
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD


def _dispatch_to_specialists(state: ReviewState) -> list[Send]:
    return [Send(name, {**state, "specialist_name": name}) for name in SPECIALIST_NAMES]


def _make_real_specialist_node(name: str, deps: GraphDeps):
    async def specialist_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        diff = DiffContext(
            repo_full_name=state["repo_full_name"],
            pr_number=state["pr_number"],
            diff_text=state["diff_text"],
        )
        findings = await deps.agents[name].review(diff, review_id=state.get("review_id"))
        return {
            "specialist_results": [
                {"agent": name, "findings": [f.model_dump(mode="json") for f in findings]}
            ]
        }

    return specialist_node


def _make_real_aggregate_node(deps: GraphDeps):
    async def aggregate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        by_agent: dict[str, list[Finding]] = {}
        for entry in state["specialist_results"]:
            by_agent[entry["agent"]] = [Finding.model_validate(d) for d in entry["findings"]]
        findings_by_agent = [by_agent.get(name, []) for name in SPECIALIST_NAMES]

        result = await nodes.aggregate(
            repo_full_name=state["repo_full_name"],
            pr_number=state["pr_number"],
            delivery_id=state["delivery_id"],
            findings_by_agent=findings_by_agent,
            github=deps.github,
            truth_store=deps.truth_store,
            hitl=deps.hitl,
            events=deps.events,
            threshold=deps.threshold,
        )
        return {
            "aggregated": {
                "review_id": result.review_id,
                "decision": result.decision.value,
                "overall_confidence": result.overall_confidence,
                "github_review_id": result.github_review_id,
                "hitl_review_id": result.hitl_review_id,
                "findings": len(result.deduped_findings),
            }
        }

    return aggregate_node


def build_review_graph(deps: GraphDeps) -> StateGraph:
    """The real review graph: 4 M4 specialists fanned out, joined at
    `nodes.aggregate`. Compilation (with a checkpointer) happens in
    `LangGraphEngine`."""
    graph = StateGraph(ReviewState)
    for name in SPECIALIST_NAMES:
        graph.add_node(name, _make_real_specialist_node(name, deps))
        graph.add_edge(name, "aggregate")
    graph.add_node("aggregate", _make_real_aggregate_node(deps))
    graph.add_conditional_edges(START, _dispatch_to_specialists, list(SPECIALIST_NAMES))
    graph.add_edge("aggregate", END)
    return graph


# ── the stub (M3) graph — orchestration-mechanics tests only ────────────────


def _make_specialist_node(name: str):
    async def specialist_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        hooks = (config.get("configurable") or {}).get("test_hooks") or {}
        if on_start := hooks.get("on_start"):
            on_start(name, time.monotonic())
        if should_crash := hooks.get("should_crash"):
            if should_crash(name):
                raise RuntimeError(f"simulated crash in specialist node '{name}'")
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
    """The M3 stub graph — no real work, just the fan-out/join shape for the
    checkpoint-resume mechanics tests."""
    graph = StateGraph(ReviewState)
    for name in SPECIALIST_NAMES:
        graph.add_node(name, _make_specialist_node(name))
        graph.add_edge(name, "aggregate")
    graph.add_node("aggregate", _aggregate)
    graph.add_conditional_edges(START, _dispatch_to_specialists, list(SPECIALIST_NAMES))
    graph.add_edge("aggregate", END)
    return graph
