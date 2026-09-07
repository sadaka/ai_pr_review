"""The aggregator + confidence-weighted HITL gate (L7).

Runs after the four specialists. It:
  1. merges the four `Finding` lists into one,
  2. deduplicates findings that multiple agents raised on the same file+line
     (keeps the highest-confidence one, notes which agents agreed),
  3. computes an `overall_confidence` — the mean of the surviving findings'
     confidence, or 1.0 when there is nothing to report,
  4. applies the gate:
       - any CRITICAL finding        → escalate to a human (regardless of confidence)
       - overall_confidence < 0.75   → route to the human approval queue
       - otherwise                   → post the review to GitHub automatically
  5. persists the review + findings to the truth lane either way.

`structured_output_only`: everything here operates on validated `Finding`
objects, never raw model prose. The collaborators (`github`, `truth_store`,
`hitl`) are injected — this module names only narrow Protocols, so it does not
import the `integrations` / `hitl` implementations and the test doubles satisfy
it without ceremony.

M5 scope note: `aggregate()` is not yet wired into `orchestrator/graph.py` (the
graph still runs the M3 stub `_aggregate`). Swapping the stub for this — and
the stub specialists for the real M4 agents — is a later milestone. M5 builds
and proves this unit against `Finding` lists directly.
"""
from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from agents.contracts import Finding, Severity
from observability.events import DECISION, NullEventSink, SupportsEmit

DEFAULT_CONFIDENCE_THRESHOLD = 0.75

_SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
)


class Decision(str, Enum):
    AUTO_POST = "auto_post"
    QUEUED = "queued"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class AggregateResult:
    review_id: str
    overall_confidence: float
    decision: Decision
    github_review_id: int | None
    hitl_review_id: str | None
    deduped_findings: list[Finding]


class SupportsPostReview(Protocol):
    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int: ...


class SupportsTruthStore(Protocol):
    async def upsert_review(self, *, repo: str, pr_number: int, delivery_id: str): ...
    async def insert_findings(self, *, review_id: str, findings: Sequence[Finding]) -> None: ...
    async def mark_posted(self, *, review_id: str, github_review_id: int, overall_confidence: float) -> None: ...
    async def set_status(self, *, review_id: str, status: str, overall_confidence: float) -> None: ...


class SupportsHitlQueue(Protocol):
    async def enqueue(self, *, review_id: str, reason: str) -> str: ...


# ── pure logic ──────────────────────────────────────────────────────────────


def dedup_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Collapse findings sharing an exact (file, line) to the single
    highest-confidence one. When more than one agent flagged the same spot,
    append an agreement note to the surviving finding's rationale — cross-agent
    agreement is signal a human reviewer wants to see."""
    groups: dict[tuple[str, int | None], list[Finding]] = {}
    for f in findings:
        groups.setdefault((f.file, f.line), []).append(f)

    out: list[Finding] = []
    for group in groups.values():
        winner = max(group, key=lambda f: f.confidence)
        if len(group) > 1:
            others = sorted({f.agent_type.value for f in group} - {winner.agent_type.value})
            if others:
                note = f"\n\n_Also flagged by: {', '.join(others)}._"
                winner = winner.model_copy(update={"rationale": winner.rationale + note})
        out.append(winner)

    # stable, useful ordering for the posted body: severity then file
    out.sort(key=lambda f: (_SEVERITY_ORDER.index(f.severity), f.file, f.line or 0))
    return out


def overall_confidence(findings: Sequence[Finding]) -> float:
    """Mean confidence of the findings; 1.0 when there is nothing to report
    (an empty review is a maximally-confident 'looks clean')."""
    if not findings:
        return 1.0
    return statistics.fmean(f.confidence for f in findings)


def decide(
    findings: Sequence[Finding],
    score: float,
    *,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> Decision:
    if any(f.severity is Severity.CRITICAL for f in findings):
        return Decision.ESCALATED
    if score < threshold:
        return Decision.QUEUED
    return Decision.AUTO_POST


def render_review_body(findings: Sequence[Finding]) -> str:
    if not findings:
        return "✅ No issues found by the automated review."

    lines = ["**Automated review**", ""]
    for severity in _SEVERITY_ORDER:
        bucket = [f for f in findings if f.severity is severity]
        if not bucket:
            continue
        lines.append(f"### {severity.value.upper()}")
        for f in bucket:
            loc = f.file if f.line is None else f"{f.file}:{f.line}"
            lines.append(
                f"- **{f.title}** — `{loc}` · confidence {f.confidence:.2f} · {f.agent_type.value}"
            )
            for rationale_line in f.rationale.splitlines():
                lines.append(f"  > {rationale_line}" if rationale_line else "  >")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── the orchestrating step ──────────────────────────────────────────────────


async def aggregate(
    *,
    repo_full_name: str,
    pr_number: int,
    delivery_id: str,
    findings_by_agent: Sequence[Sequence[Finding]],
    github: SupportsPostReview,
    truth_store: SupportsTruthStore,
    hitl: SupportsHitlQueue,
    events: SupportsEmit | None = None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> AggregateResult:
    merged: list[Finding] = [f for agent_findings in findings_by_agent for f in agent_findings]
    deduped = dedup_findings(merged)
    score = overall_confidence(deduped)

    review = await truth_store.upsert_review(
        repo=repo_full_name, pr_number=pr_number, delivery_id=delivery_id
    )

    # Idempotency: a redelivered webhook resolves to the same review row. If it
    # is no longer 'pending', the gate already ran once — don't repeat any side
    # effect (double GitHub review, duplicate HITL row).
    if review.status != "pending":
        prior = {
            "posted": Decision.AUTO_POST,
            "needs_human": Decision.QUEUED,
            "escalated": Decision.ESCALATED,
        }.get(review.status, Decision.QUEUED)
        return AggregateResult(
            review_id=review.id,
            overall_confidence=score,
            decision=prior,
            github_review_id=review.github_review_id,
            hitl_review_id=None,
            deduped_findings=deduped,
        )

    await truth_store.insert_findings(review_id=review.id, findings=deduped)
    decision = decide(deduped, score, threshold=threshold)

    sink: SupportsEmit = events or NullEventSink()
    await sink.emit(
        review_id=review.id,
        agent="aggregator",
        event_type=DECISION,
        outcome=decision.value,
        confidence=score,
        payload={"findings": len(deduped), "threshold": threshold},
    )

    if decision is Decision.AUTO_POST:
        event = "APPROVE" if not deduped else "COMMENT"
        github_review_id = await github.post_review(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            body=render_review_body(deduped),
            event=event,
        )
        await truth_store.mark_posted(
            review_id=review.id, github_review_id=github_review_id, overall_confidence=score
        )
        return AggregateResult(review.id, score, decision, github_review_id, None, deduped)

    if decision is Decision.ESCALATED:
        crits = [f for f in deduped if f.severity is Severity.CRITICAL]
        reason = f"{len(crits)} CRITICAL finding(s): " + "; ".join(f.title for f in crits)
        status = "escalated"
    else:  # QUEUED
        reason = f"overall confidence {score:.2f} below threshold {threshold:.2f}"
        status = "needs_human"

    hitl_review_id = await hitl.enqueue(review_id=review.id, reason=reason)
    await truth_store.set_status(review_id=review.id, status=status, overall_confidence=score)
    return AggregateResult(review.id, score, decision, None, hitl_review_id, deduped)
