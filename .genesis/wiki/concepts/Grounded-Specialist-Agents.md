---
title: Grounded Specialist Agents & the Finding Contract
filed: 2026-09-06
milestone: M4
---

# Grounded Specialist Agents & the Finding Contract

**What it is:** `backend/agents/` (the four specialists + shared base) +
`backend/memory/context_retriever.py` (hybrid retrieval) + `backend/prompts/`
(per-agent system prompts). M4 fills in the real bodies behind M3's four stub
specialist nodes — the [[LangGraph-Orchestrator]] graph shape is unchanged.

Each specialist (`security`, `quality`, `tests`, `docs`) is *only* two class
attributes: `agent_type` and `system_prompt = load_prompt("<name>")`. The
whole pipeline — retrieve grounding → build prompt → structured LLM call →
stamp identity → validate — lives once in `SpecialistAgent.review()`. A new
specialist is a prompt file plus a 3-line subclass.

## The two-tier Finding contract

`agents/contracts.py` has two schemas, deliberately:

- **`FindingDraft`** — what the LLM is asked to produce (`severity`,
  `category`, `file`, `line`, `confidence`, `title`, `rationale`). No
  `agent_type`.
- **`Finding`** — the real contract everything downstream (M5 aggregator,
  HITL queue, GitHub posting) consumes. `FindingDraft` + `agent_type`.

`base_agent` builds the second from the first: `Finding.from_draft(draft,
agent_type=self.agent_type)`. `agent_type` is stamped from the agent's own
class attribute, never requested from the model. Reasons: it's redundant (the
security agent knows it's the security agent), it's a field the model can get
wrong, and — since a diff under review is untrusted input — a prompt-injected
diff must not be able to make one agent's findings masquerade as another's.
General rule: **the LLM produces judgment; code stamps facts.** This is the
`structured_output_only` invariant in practice — only validated `Finding`
objects leave `review()`, never prose.

## Failure is an empty list, never an exception

When the structured parse returns `None` (model refused, hit a length cap
mid-JSON, or output won't coerce), `review()` returns `[]`. One specialist
must not crash the M3 parallel fan-out/join, and for a CI tool a missing
comment beats a crashed check. **Known gap:** this is silent — a
persistently-refusing agent looks identical to a genuinely-clean one until M6
emits `llm.call` outcomes to `agent_events`.

## Hybrid retrieval (the "grounded" part)

`context_retriever.py` is the *only* module that reads `code_chunks`
(`single_data_spine`). One SQL statement runs two rankers over the repo's
chunks — DiskANN vector similarity (`embedding <=> $1`) and Postgres FTS
(`plainto_tsquery` / `ts_rank_cd`) — and fuses them by reciprocal rank fusion
(`1 / (RRF_K + rank)`, `RRF_K = 60`) via a `FULL OUTER JOIN`. Specialists
reason over retrieved codebase context, not the diff in isolation — this is
the ADR (see `ai-pr-review-agent.html` L4) that separates this from a
single-LLM diff reviewer.

Embeddings: OpenAI `text-embedding-3-small` with `dimensions=256` (Matryoshka
truncation) to match the `code_chunks.embedding vector(256)` column from the
M1 migration.

## Provider choice

Runs on **OpenAI** (`openai` SDK — `chat.completions.parse` with a Pydantic
`response_format`, plus `embeddings.create`). `anthropic` + `voyageai` sit in
`pyproject.toml` unused/reserved. A switch would move `llm_client.py` +
`base_agent.py` to the Anthropic SDK and embeddings to Voyage (Anthropic has
no embeddings API), and require a full re-embed of `code_chunks` — embedding
spaces are not interchangeable. See `M4-llm-provider-openai` in
`implementation-notes.html`.

## Test-harness note

The live e2e test (`test_specialists_e2e.py`) pins the whole module to one
event loop (`loop_scope="module"` on the marker and the async fixtures)
because its module-scoped `asyncpg` pool binds to the loop alive at creation
time — pytest-asyncio's default per-function loop would close underneath it
(`Future attached to a different loop`).

See also: [[LangGraph-Orchestrator]], [[Webhook-Ingress-and-Job-Queue]]
