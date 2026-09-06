"""M3 demo: pytest backend/tests/test_orchestrator.py -k
"parallel_fanout and checkpoint_resume_after_crash" """
from __future__ import annotations

import time

import fakeredis
import pytest

from orchestrator.langgraph_engine import LangGraphEngine

INITIAL_STATE = {"pr_number": 7, "repo_full_name": "octocat/hello-world", "specialist_results": []}


@pytest.fixture
def shared_redis_server():
    """One fake Redis "server" shared across multiple client connections —
    simulates multiple engine instances (processes) talking to the same
    external Redis, which is exactly what crash/resume needs to prove."""
    return fakeredis.FakeServer()


def new_engine(server: fakeredis.FakeServer) -> LangGraphEngine:
    return LangGraphEngine(fakeredis.FakeAsyncRedis(server=server))


async def test_parallel_fanout_all_four_specialists_run_concurrently(shared_redis_server):
    engine = new_engine(shared_redis_server)
    intervals: dict[str, tuple[float, float]] = {}

    def on_start(name: str, t: float) -> None:
        intervals.setdefault(name, [None, None])[0] = t

    def on_complete(name: str, t: float) -> None:
        intervals[name][1] = t

    result = await engine.run(
        INITIAL_STATE,
        thread_id="thread-fanout",
        test_hooks={"delay_seconds": 0.05, "on_start": on_start, "on_complete": on_complete},
    )

    assert result["aggregated"]["agent_count"] == 4
    assert result["aggregated"]["agents"] == ["docs", "quality", "security", "tests"]
    assert set(intervals.keys()) == {"security", "quality", "tests", "docs"}

    # Overlap proof: every specialist's [start, end) interval overlaps every
    # other's. If they ran sequentially, consecutive intervals wouldn't overlap.
    names = list(intervals.keys())
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            a_start, a_end = intervals[a]
            b_start, b_end = intervals[b]
            assert a_start < b_end and b_start < a_end, f"{a} and {b} did not run concurrently"

    # Wall-clock proof: 4 nodes at 0.05s each ran well under 4x0.05s serial time.
    total_span = max(end for _, end in intervals.values()) - min(start for start, _ in intervals.values())
    assert total_span < 0.15  # comfortably below 4 * 0.05s = 0.2s serial time


async def test_parallel_fanout_and_checkpoint_resume_after_crash(shared_redis_server):
    """Single test matching the M3 demo command's -k filter verbatim: proves
    both success criteria together on one run (parallel fan-out, then a
    simulated crash + resume that doesn't re-run completed nodes)."""
    run_calls: list[str] = []
    completed_before_crash = {"security", "quality"}

    def on_start(name: str, _t: float) -> None:
        run_calls.append(name)

    def should_crash(name: str) -> bool:
        return name not in completed_before_crash

    engine = new_engine(shared_redis_server)
    # No artificial delay here: the completing nodes (security/quality) need to
    # actually finish and have their writes persisted before the crashing
    # siblings' exception aborts the run. A delay on the completing side risks
    # them getting cancelled mid-sleep instead of finishing — see
    # test_checkpoint_resume_after_crash_does_not_rerun_completed_nodes, which
    # is deliberately delay-free for the same reason.
    with pytest.raises(RuntimeError, match="simulated crash"):
        await engine.run(
            INITIAL_STATE,
            thread_id="thread-e2e",
            test_hooks={"on_start": on_start, "should_crash": should_crash},
        )
    assert completed_before_crash.issubset(set(run_calls))  # parallel fan-out really started all 4
    calls_before_resume = list(run_calls)

    resumed_engine = new_engine(shared_redis_server)
    result = await resumed_engine.run(INITIAL_STATE, thread_id="thread-e2e", test_hooks={"on_start": on_start})

    assert result["aggregated"]["agent_count"] == 4
    assert result["aggregated"]["agents"] == ["docs", "quality", "security", "tests"]
    # NOTE: both crashing AND completing specialists call on_start before the
    # crash (should_crash is checked after on_start), so calls_before_resume
    # already contains all 4 names — `set(after) - set(before)` would always
    # be empty and could never catch a completed node being re-run. Slicing
    # the post-resume tail of the call list (rather than set-differencing the
    # whole cumulative history) is what actually detects a re-run.
    calls_after_resume = run_calls[len(calls_before_resume) :]
    rerun_completed = [name for name in calls_after_resume if name in completed_before_crash]
    assert rerun_completed == [], f"completed nodes were re-run after resume: {rerun_completed}"


async def test_checkpoint_resume_after_crash_does_not_rerun_completed_nodes(shared_redis_server):
    run_calls: list[str] = []

    def on_start(name: str, _t: float) -> None:
        run_calls.append(name)

    completed_before_crash = {"security", "quality"}

    def should_crash(name: str) -> bool:
        # Let exactly 2 of the 4 specialists finish, then blow up a 3rd — the
        # 4th's task/branch was never scheduled to completion either, since
        # the whole ainvoke aborts once any parallel branch raises.
        return name not in completed_before_crash

    engine_before_crash = new_engine(shared_redis_server)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await engine_before_crash.run(
            INITIAL_STATE,
            thread_id="thread-crash",
            test_hooks={"on_start": on_start, "should_crash": should_crash},
        )

    assert completed_before_crash.issubset(set(run_calls))
    calls_before_resume = list(run_calls)

    # Simulate a fresh process: a brand-new engine instance, same external
    # Redis, same thread_id, no crash injected this time.
    engine_after_crash = new_engine(shared_redis_server)
    result = await engine_after_crash.run(
        INITIAL_STATE,
        thread_id="thread-crash",
        test_hooks={"on_start": on_start},
    )

    assert result["aggregated"]["agent_count"] == 4
    assert result["aggregated"]["agents"] == ["docs", "quality", "security", "tests"]

    # The 2 specialists that completed before the crash must NOT have been
    # re-run after resume — only whatever was still outstanding. Slicing the
    # post-resume tail (not set-differencing the cumulative history) is what
    # actually detects a re-run: both crashing and completing nodes call
    # on_start before the crash, so `calls_before_resume` already contains
    # all 4 names, and a naive set difference would always come up empty.
    calls_after_resume = run_calls[len(calls_before_resume) :]
    rerun_completed = [name for name in calls_after_resume if name in completed_before_crash]
    assert rerun_completed == [], f"completed nodes were re-run after resume: {rerun_completed}"
