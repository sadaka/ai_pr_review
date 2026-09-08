"""LangGraph implementation of `core.workflow_engine.WorkflowEngine` — the one
module allowed to import LangGraph directly (ADR-001). Everything else talks
to `WorkflowEngine`.
"""
from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from redis.asyncio import Redis

from core.workflow_engine import WorkflowEngine
from orchestrator.graph import GraphDeps, build_graph, build_review_graph
from orchestrator.redis_checkpointer import RedisSaver


class LangGraphEngine(WorkflowEngine):
    """Compiles the review graph once with a Redis-backed checkpointer.
    `thread_id` is passed straight through as LangGraph's own thread id, so
    calling `run` again with the same id resumes the same checkpointed run.

    With `deps` it compiles the real M11 pipeline (4 M4 specialists →
    `nodes.aggregate`); without, the M3 stub graph (mechanics tests only).
    """

    def __init__(self, redis: Redis, *, deps: GraphDeps | None = None) -> None:
        self._checkpointer = RedisSaver(redis)
        graph = build_review_graph(deps) if deps is not None else build_graph()
        self._compiled = graph.compile(checkpointer=self._checkpointer)

    async def run(
        self,
        initial_state: dict[str, Any],
        *,
        thread_id: str,
        test_hooks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run/resume the review graph for `thread_id`. `test_hooks` is not part
        of the `WorkflowEngine` interface — it's a LangGraph-specific escape
        hatch (passed via `config["configurable"]`, never persisted) that lets
        tests observe or fault-inject individual node runs without touching
        graph state. Production callers never pass it."""
        config: RunnableConfig = {"configurable": {"thread_id": thread_id, "test_hooks": test_hooks or {}}}

        existing = await self._checkpointer.aget_tuple(config)
        # `None` is LangGraph's actual resume signal: it tells the compiled graph
        # to continue from whatever is pending in the checkpoint rather than
        # dispatching a fresh run from START. Passing `{}` (or any other input)
        # here would look like a new invocation and re-run everything, including
        # nodes that already completed — exactly the bug crash-resume exists to
        # prevent.
        state = initial_state if existing is None else None
        return await self._compiled.ainvoke(state, config=config)
