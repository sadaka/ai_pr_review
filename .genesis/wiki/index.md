# Wiki Index — ai-pr-review

The project knowledge base. Same schema as the agentic-swe-kit wiki: concept pages in `concepts/`,
each with frontmatter and ≥2 `[[wikilinks]]`. The L3 RESEARCH loop writes here; G0 reads here first.

> **Read this file before any milestone (G0 step 1).** Pick candidate pages by name-matching the
> milestone's nouns, then drill in. The wiki is what prevents rebuilding work that already exists.

## Entities (the things this system has)
<!-- - [[concepts/<Entity>]] — one-line summary -->

## Concepts (how it works)
- [[concepts/Webhook-Ingress-and-Job-Queue]] — FastAPI /webhook → HMAC verify → dedup → ARQ enqueue, M2
- [[concepts/Idempotent-Retry-Ordering]] — claim-before-act + rollback pattern used at the webhook/queue boundary, M2
- [[concepts/LangGraph-Orchestrator]] — Send-API fan-out to 4 stub specialists + custom Redis checkpointer + crash-resume, M3
- [[concepts/Grounded-Specialist-Agents]] — the 4 real specialists, two-tier Finding contract, hybrid DiskANN+FTS+RRF retrieval, M4

## Sources (research distilled by L3)
<!-- - [[concepts/<source-slug>]] — one-line summary | filed <date> -->

## Seeded from agentic-swe-kit
Relevant global concept pages for this project's phases (pointers only — read on demand):

- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Parallel-and-Fan-Out-Agents.md — the four-specialist fan-out (security/quality/tests/docs), Phase 4/8
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Agent-Fundamentals.md — base agent shape, tool calls, structured output, Phase 5
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Evaluation-Frameworks.md — golden dataset + LLM-as-judge for the review agent, Phase 9
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Observability-and-Cost-Control.md — agent_events spine, BudgetGuard, Phase 10/16
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Production-Hardening.md — reliability layer (retries, circuit breakers, idempotency), Phase 12
- $AGENTIC_SWE_WIKI_ROOT/clean-architecture/concepts/Dependency-Rule.md — inward-only dependency direction for the 23-module monolith, Phase 1 (ADR-002)
- $AGENTIC_SWE_WIKI_ROOT/clean-architecture/concepts/Deferred-Framework-Commitment.md — the `core/workflow_engine.py` interface hiding LangGraph vs Temporal, Phase 1 (ADR-001)
- $AGENTIC_SWE_WIKI_ROOT/clean-architecture/concepts/Database-as-Detail.md — why the Tiger Cloud store sits behind repository interfaces, Phase 1/13
- $AGENTIC_SWE_WIKI_ROOT/designing-data-intensive-applications/concepts/Polyglot-Persistence.md — the "one store, three lanes" decision (ADR-003), Phase 6/13/14
- $AGENTIC_SWE_WIKI_ROOT/designing-data-intensive-applications/concepts/Encoding-and-Schema-Evolution.md — Finding contract schema stability across agent versions, Phase 5/14
- $AGENTIC_SWE_WIKI_ROOT/distributed-systems/concepts/Fault-Tolerance.md — orchestration-deadlock and timeout defenses at the aggregator join, Phase 4/12
- $AGENTIC_SWE_WIKI_ROOT/security-engineering/concepts/Threat-Modeling.md — prompt injection via untrusted diff content, GitHub App trust boundary, Phase 11
