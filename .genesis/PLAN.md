# PLAN — ai-pr-review

The machine-parseable implementation plan. Mirrors the milestone table in `DONE.html` (DONE.html is the
human/visual view; this is the one loops read). Sliced so each milestone ships in one L1 BUILD pass.

> Slicing rule: a milestone must have (a) a single clear outcome, (b) an exact **demo command** that
> proves it, and (c) a freeze boundary of files it may touch. If you can't write the demo command,
> the milestone is too vague — split it.

---

## Brainstorm (G0.5 — fill before slicing milestones)

> Three fundamentally different approaches to the cognitive job. Pick one. Record the rationale.
> This is the cheapest design decision — you haven't written a line of code yet.

### Approach A — Single-LLM review
One prompt judges the whole diff in one pass.
- Strengths: Trivial to build, cheap, fast to demo.
- Weaknesses: One mindset collapses four distinct concerns (security/quality/tests/docs); no codebase grounding; hallucinates with confidence; no audit trail; no selectivity.

### Approach B — Static analysis + rules engine
Deterministic linters and data-flow analysis, no LLM.
- Strengths: Zero hallucination, fully reproducible, cheap to run.
- Weaknesses: No semantics — cannot judge intent, logic correctness, or whether a test is meaningful; high false-positive rate; cannot read documentation intent.

### Approach C — Agentic fan-out with grounded specialists (chosen)
Four parallel specialist agents (security, quality, tests, docs), each grounded via hybrid RAG retrieval over the codebase, merged by an aggregator that applies a confidence-weighted HITL gate before posting.
- Strengths: Each concern reasoned deeply, not shallowly; grounding removes the dominant hallucination failure mode; structured Finding contract makes results auditable and disputable; selective posture (L0) — surfaces only what's worth a senior's attention.
- Weaknesses: Requires orchestration (LangGraph), a retrieval layer, and a proof/observability layer — real infrastructure, not a single prompt.

### Chosen: Approach C — Agentic fan-out. Derived first-principles in `ai-pr-review-agent.html` (L0–L9): grounding (L4) is what separates this from a single-LLM reviewer, and the four concerns (L1) mirror how a senior engineer actually reviews. This is the only approach that supports the confidence-weighted HITL gate (L7) and the audit/proof requirement (L6).

---

## Milestones

> Reordered 2026-08-22 per user request: data infra first (independent of everything else), then
> ingestion, then orchestration/agents, with the frontend deliberately minimal and pushed late — a
> single status page, not a build focus. Each milestone lists exactly which credentials it needs so
> you can get each one only when you actually reach it. See the credentials walkthrough in
> `.genesis/checkpoints/CREDENTIALS.md`.

### M1 — Tiger Cloud data spine provisioned
- **Outcome:** Tiger Cloud service is live; `2026-06-tiger-init.sql` applied; the three lanes exist — `code_chunks` (pgvector/pgvectorscale/DiskANN + FTS), `agent_events` hypertable, and the continuous aggregates (`agent_health_1m`, `pr_cost_hourly`).
- **Phase (swe-master):** Phase 13/14 — Infrastructure & Data Engineering (Tiger integration Phase A, ADR-003)
- **Files / freeze boundary:** `backend/database/**`, `scripts/migrations/2026-06-tiger-init.sql`
- **Credentials needed:** `TIGER_DATABASE_URL` only.
- **Demo command:** `psql "$TIGER_DATABASE_URL" -c "\dx" | grep -E "timescaledb|vector|vectorscale" && psql "$TIGER_DATABASE_URL" -c "SELECT hypertable_name FROM timescaledb_information.hypertables;" | grep agent_events`
- **Success criteria:** All three extensions listed; `agent_events` reports as a hypertable; both continuous aggregate views exist in `information_schema.views`.
- **Loops:** L1, L4
- **Skills:** canon + tdd + data-systems-engineering
- **Token budget:** 50000

### M2 — Webhook ingress + queue
- **Outcome:** FastAPI receives a GitHub `pull_request` webhook, verifies its HMAC-SHA256 signature, dedups on the `X-GitHub-Delivery` idempotency key, and enqueues a review job to Redis/ARQ — returning 200 before any review work happens.
- **Phase:** Phase 3 — Backend & API
- **Files:** `backend/webhook_receiver/**`, `backend/job_queue/**`, `backend/api/schemas.py`
- **Credentials needed:** `REDIS_URL`, `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY_PATH`, and a smee.io URL for local webhook delivery.
- **Demo command:** `pytest backend/tests/test_webhook_ingress.py -k "valid_signature and idempotent_replay and enqueues_job"`
- **Success criteria:** Valid signed payload → 200 + one job in the ARQ queue. Replayed delivery ID → 200 + no second job. Bad signature → 401, nothing enqueued.
- **Loops:** L1, L4
- **Skills:** canon + tdd + data-systems-engineering
- **Token budget:** 50000

### M3 — LangGraph orchestrator: parallel fan-out + checkpointing
- **Outcome:** `core/workflow_engine.py` interface + `orchestrator/langgraph_engine.py` implementation. A StateGraph fans out to four stub specialist nodes via the Send API, joins at a stub aggregator, checkpoints state to Redis at each node boundary, and resumes correctly after a simulated mid-run crash.
- **Phase:** Phase 4 — Workflow Orchestration
- **Files:** `backend/core/workflow_engine.py`, `backend/orchestrator/**`
- **Credentials needed:** none beyond M1/M2 (`TIGER_DATABASE_URL`, `REDIS_URL`) — the 4 nodes are stubs here, no LLM calls yet.
- **Demo command:** `pytest backend/tests/test_orchestrator.py -k "parallel_fanout and checkpoint_resume_after_crash"`
- **Success criteria:** All 4 stub nodes observed running concurrently (overlapping start/end timestamps in test); killing the worker after 2/4 nodes complete and resuming reaches the aggregator without re-running the completed 2.
- **Loops:** L1, L4
- **Skills:** canon + tdd + distributed-systems
- **Token budget:** 50000

### M4 — Grounded specialist agents + Finding contract
- **Outcome:** `agents/contracts.py` (Finding schema) + `agents/base_agent.py` + the four specialists (security, quality, tests, docs). Each queries `memory/context_retriever.py` (hybrid DiskANN + FTS over `code_chunks`) and returns a list of schema-valid `Finding` objects for a real diff.
- **Phase:** Phase 5/6/8 — LLM & Reasoning, Memory Architecture, Multi-Agent Systems
- **Files:** `backend/agents/**`, `backend/memory/**`, `backend/prompts/**`
- **Credentials needed:** `OPENAI_API_KEY` (embeddings + LLM calls), plus M1's `TIGER_DATABASE_URL`.
- **Demo command:** `pytest backend/tests/test_specialists_e2e.py -k "seeded_repo_diff_produces_valid_findings"`
- **Success criteria:** Against a seeded `code_chunks` fixture repo and a known-vulnerable diff fixture, all 4 agents return ≥1 `Finding` each, every `Finding` validates against the Pydantic schema, and the security agent's findings include the seeded SQL-injection line.
- **Loops:** L1, L3 (research), L4
- **Skills:** canon + tdd + llmops-ai-agents
- **Token budget:** 50000

### M5 — Aggregator, HITL gate, GitHub posting
- **Outcome:** `orchestrator/nodes.py:aggregate` merges the four Finding lists, dedups same file/line findings across agents, computes `overall_confidence`, and applies the confidence-weighted HITL gate (L7): auto-post via `integrations/github_client.py` when confident and CRITICAL-free, else insert into `hitl/queue.py`.
- **Phase:** Phase 3/7/19 — Backend & API, Tooling, Human-in-the-Loop
- **Files:** `backend/orchestrator/nodes.py`, `backend/hitl/**`, `backend/integrations/**`
- **Credentials needed:** none new — reuses M2's GitHub App credentials to post.
- **Demo command:** `pytest backend/tests/test_aggregator_hitl_e2e.py -k "high_confidence_auto_posts and low_confidence_queues and critical_always_escalates"`
- **Success criteria:** High-confidence/no-CRITICAL fixture → `github_client.post_review` called once, nothing in HITL queue. Low-confidence fixture → no post, one row in `hitl_reviews`. Any-CRITICAL fixture → no post regardless of confidence, escalation row written.
- **Loops:** L1, L2, L4
- **Skills:** canon + tdd + production-readiness
- **Token budget:** 50000

### M6 — Observability spine + cost ledger + BudgetGuard
- **Outcome:** `observability/events.py:emit_agent_event` wired into every orchestrator node and the LLM client, landing rows in `agent_events`. `economics/budget.py` BudgetGuard reads `agent_health_1m` at the top of every agent run and hard-blocks before the next LLM call if the daily cap is exceeded (ADR-004).
- **Phase:** Phase 10/16 — Observability & Tracing, Economics & Cost Control (Tiger integration Phase B)
- **Files:** `backend/observability/**`, `backend/economics/**`
- **Credentials needed:** none new.
- **Demo command:** `pytest backend/tests/test_observability_budget_e2e.py -k "full_review_traces_to_agent_events and budget_guard_blocks_over_cap"` `&& psql "$TIGER_DATABASE_URL" -c "SELECT count(*) FROM agent_events WHERE review_id = :last_test_review_id;"`
- **Success criteria:** Running M5's pipeline produces span.start/llm.call/tool.call/span.end/decision rows in `agent_events` for all 4 agents + aggregator, queryable by `review_id`. Setting the daily cap below the cost of one call causes the next agent run to raise `BudgetExceeded` before any LLM call fires.
- **Loops:** L1, L4
- **Skills:** canon + tdd + llmops-ai-agents
- **Token budget:** 50000

### M7 — Minimal status frontend (deliberately small — do not over-invest here)
- **Outcome:** One Next.js page, no design system, no polish pass. It reads the M6 aggregates and lists recent reviews with cost/confidence/status. Exists so there's something to look at, not as a build focus.
- **Phase:** Phase 2 — Frontend Engineering (kept intentionally thin)
- **Files:** `frontend/src/app/page.tsx`, `frontend/src/lib/api.ts` only — no component library, no auth UI, no design-system skill invoked.
- **Credentials needed:** none new — reads the same backend API M5/M6 already expose.
- **Demo command:** `curl -s http://localhost:3000 | grep -q "Reviews"`
- **Success criteria:** Page renders a table of reviews (id, status, cost, confidence) fetched from `/api/reviews`. That's it — no styling gate, no responsive-design gate.
- **Loops:** L1, L4
- **Skills:** canon + tdd only — explicitly skip the design-system skill for this milestone
- **Token budget:** 20000 — capped deliberately lower than the other milestones

<!-- M8+ (evaluation gate, threat model + injection guard, reliability under fault injection,
     CI/CD eval gates, continuous learning) — slice after M7 lands, per the
     20-phase roadmap in ai-pr-review-agent.html §4.1 -->

---

## Progress (loops append here on milestone completion — newest last)

- 2026-08-22 · **M1 DONE** — Tiger Cloud data spine provisioned. Migration `scripts/migrations/2026-06-tiger-init.sql` applied against the live service: `code_chunks` (pgvector/pgvectorscale/DiskANN + FTS), `agent_events` hypertable, `agent_health_1m` + `pr_cost_hourly` continuous aggregates, plus the truth-lane tables. L4 VERIFY (fresh agent, no build trail): APPROVE, one fix applied (explicit `timescaledb_toolkit` extension, previously implicit). Quiz-me: 2/3 correct — DiskANN-vs-HNSW rationale corrected in `checkpoints/M1.md`. Next: M2.
- 2026-09-04 · **M2 DONE** — Webhook ingress + queue. `backend/webhook_receiver/` (FastAPI `/webhook`, HMAC-SHA256 signature verification, `X-GitHub-Delivery` dedup) + `backend/job_queue/` (ARQ enqueue, Redis dedup claim/rollback, retry-with-backoff + circuit breaker) + `backend/api/schemas.py`. Also fixed two pre-existing `backend/.env` bugs (corrupted `REDIS_URL`, wrong `GITHUB_PRIVATE_KEY_PATH`). L4 VERIFY round 1: REJECT (missing retry-with-backoff, connect-timeout-only, job-loss race on failed enqueue after dedup claim). All 4 required fixes applied + regression test added. L4 VERIFY round 2: APPROVE. Quiz-me: Q1 corrected (dedup-ordering is about idempotency-on-retry, not GitHub-timeout avoidance), Q2/Q3 confirmed understood. 13/13 tests green, mypy clean, demo command passes verbatim. Next: M3.
- 2026-09-06 · **M4 DONE** — Grounded specialist agents + Finding contract. `backend/agents/{contracts,base_agent,llm_client}.py` + 4 specialists (`security/quality/tests/docs`) + `backend/prompts/*.md` + `backend/memory/context_retriever.py` (hybrid DiskANN vector + Postgres FTS over `code_chunks`, combined by RRF) + `backend/tests/{test_base_agent,test_specialists_e2e}.py`. G0 re-check: the 2026-09-04 session had drafted the code but never checkpointed/verified it, and it was non-working — it imported `openai`, which was never installed or in `pyproject.toml` (`anthropic`+`voyageai` were declared instead). User decision: stay on OpenAI (plan updated), keep `anthropic`+`voyageai` reserved for later. Integration-only fixes: added `openai>=1.40.0`, fixed an event-loop-scope bug in the live e2e test (module-scoped asyncpg pool vs function-scoped loop → `loop_scope="module"`), one `type: ignore` on `asyncpg`. No specialist/retriever/contract logic touched. L4 VERIFY round 1: APPROVE — 26/26 tests, mypy clean on M4 dirs, demo `pytest -k seeded_repo_diff_produces_valid_findings` green against live OpenAI + Tiger Cloud (all 4 agents ≥1 `Finding`, security agent flags the seeded SQL injection). Invariants `structured_output_only` / `single_data_spine` / `dependency_direction` PASS; `outbound_call_safety` partial-acceptable (circuit breaker deferred to L8/L12 per the invariant's own text). Quiz-me: Q1 (two-tier `FindingDraft`→`Finding` split) clarified — user had answered with the maker/checker rationale; Q2/Q3 confirmed. Carry-forward noted in `checkpoints/M4.md`. Next: M5.
- 2026-09-04 · **M3 DONE** — LangGraph orchestrator: parallel fan-out + checkpointing. `backend/core/workflow_engine.py` (ADR-001 `WorkflowEngine` Protocol) + `backend/orchestrator/graph.py` (4 stub specialists fanned out via the `Send` API, stub aggregator join) + `backend/orchestrator/redis_checkpointer.py` (hand-written minimal async `BaseCheckpointSaver` against plain Redis — skipped `langgraph-checkpoint-redis`, which needs a RediSearch module Upstash's free tier doesn't have) + `backend/orchestrator/langgraph_engine.py`. L4 VERIFY round 1: REJECT — resume passed `{}` instead of `None` as the graph input, causing LangGraph to re-run ALL 4 specialists on resume instead of just the incomplete ones; the test's own rerun-check was also structurally blind to this (set-difference on cumulative call history). Both fixed (resume input corrected; tests rewritten to slice the post-resume call tail) and confirmed non-flaky across 8 repeated runs. L4 VERIFY round 2: APPROVE — verifier independently reintroduced the bug to confirm both the fix and the regression tests are load-bearing. Quiz-me: Q1 corrected (`None` = resume/continue, not "already executed"; any non-`None` input = fresh dispatch from START), Q2/Q3 confirmed understood. 16/16 tests green, mypy clean, demo command passes verbatim. Next: M4.
