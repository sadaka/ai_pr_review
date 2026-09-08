"""The review graph's shared state channel.

Kept in its own module (not inline in `graph.py`) because both the stub graph
(`build_graph`, M3 mechanics tests) and the real graph (`build_review_graph`,
M11) use the same shape, and the ARQ worker builds the initial state.

Everything here must be JSON-serialisable — LangGraph checkpoints it to Redis
at every node boundary. `Finding` objects therefore cross the boundary as
`model_dump()` dicts inside `specialist_results`; the aggregator node
re-validates them.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class ReviewState(TypedDict, total=False):
    repo_full_name: str
    pr_number: int
    delivery_id: str
    diff_text: str
    review_id: str
    # each specialist appends exactly one {"agent": name, "findings": [dump, ...]};
    # operator.add is the reducer LangGraph uses to merge the concurrent
    # parallel writes to this channel instead of one clobbering another.
    specialist_results: Annotated[list[dict[str, Any]], operator.add]
    aggregated: dict[str, Any] | None
