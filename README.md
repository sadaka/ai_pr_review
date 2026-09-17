# ai-pr-review

[![CI](https://github.com/sadaka/ai_pr_review/actions/workflows/ci.yml/badge.svg)](https://github.com/sadaka/ai_pr_review/actions/workflows/ci.yml)
[![Eval Gate](https://github.com/sadaka/ai_pr_review/actions/workflows/eval-gate.yml/badge.svg)](https://github.com/sadaka/ai_pr_review/actions/workflows/eval-gate.yml)

Self-hosted, agentic pull-request review. Four grounded specialist agents
read every PR against your actual codebase — not just the diff — and only
bother a human when it's genuinely uncertain.

---

## Table of contents

- [What is this?](#what-is-this)
- [How it works](#how-it-works)
- [Why use it?](#why-use-it)
- [How is it better than CodeRabbit / Greptile / Qodo?](#how-is-it-better-than-coderabbit--greptile--qodo)
- [Who is this for?](#who-is-this-for)
- [What it can do / what it can't (yet)](#what-it-can-do--what-it-cant-yet)
- [Install & run](#install--run)
- [Multiple repositories](#multiple-repositories)
- [Develop / test](#develop--test)
- [TODO](#todo)
- [Prompt versioning](#prompt-versioning)

---

## What is this?

`ai-pr-review` watches your GitHub repos and reviews pull requests
automatically. A GitHub webhook fans each PR's diff out to four specialist
agents — **security, quality, tests, docs** — each grounded by retrieval over
your indexed codebase, not just the raw patch text. Their findings are
merged, deduped, and confidence-scored; high-confidence results post
straight to the PR as a review comment, anything uncertain or CRITICAL waits
in a human-in-the-loop (HITL) queue you approve or reject from a small
dashboard. Every run, cost, and decision is recorded in one Postgres-
compatible data store (Tiger Cloud) you own.

It's a real, running system, not a demo: 16 shipped milestones cover data
infra, orchestration, grounded agents, aggregation + HITL, observability +
budget control, a dashboard, a security threat model + prompt-injection
guard, an evaluation/regression gate, fault-injection-tested reliability,
CI/CD, multi-repo ingestion, and auth. See [TODO](#todo) for what's
deliberately still open.

## How it works

```
GitHub ──webhook──▶ webhook_receiver ──enqueue──▶ redis ──▶ worker
                                                             │  (LangGraph: 4 specialists → aggregate)
                          frontend ◀── api (bearer-token auth) ◀┘
                                       │
                                  Tiger Cloud  (memory · events · truth · ingestion · hitl)
```

1. **Ingestion (always running in the background).** When the GitHub App is
   installed on a repo, that repo's source is fetched, chunked, and embedded
   into `code_chunks` (hybrid vector + full-text index). Every `push` after
   that incrementally re-indexes only the changed files.
2. **A PR opens or updates.** GitHub signs and sends a `pull_request`
   webhook; `webhook_receiver` verifies the HMAC signature, dedups on the
   delivery ID, and enqueues a review job to Redis.
3. **The worker fans out.** A LangGraph workflow runs the four specialists
   in parallel, each retrieving grounding context from `code_chunks` via
   hybrid DiskANN + FTS search, each returning structured `Finding` objects
   — not free text.
4. **Aggregation + the confidence gate.** Findings are deduped across
   agents, scored, and gated: any `CRITICAL` finding always escalates to
   HITL; low-confidence results are queued; everything else auto-posts as a
   single PR review.
5. **You stay in the loop.** The dashboard shows indexed repos, recent
   reviews (cost, confidence, status), and a HITL inbox with one-click
   approve/reject — approve posts the exact review that would have
   auto-posted; reject dismisses it, no GitHub call made.
6. **Everything is traced.** Every span, LLM call, tool call, and decision
   lands in an `agent_events` hypertable, joined to a live cost ledger a
   `BudgetGuard` enforces before every LLM call.

## Why use it?

- **You own the data spine.** Every finding, cost, trace, and HITL decision
  lives in your own Tiger Cloud Postgres — not a third-party SaaS backend.
  Your code and diffs never sit on someone else's servers beyond the OpenAI
  calls you already control.
- **Deep codebase grounding, not just diff-reading.** Hybrid RAG (DiskANN
  vector search + full-text search) over the whole indexed repo, so findings
  are grounded in real surrounding code, not just the patch text.
- **Four separately-reasoned specialists** (security, quality, tests, docs)
  instead of one general-purpose pass — each with its own prompt, its own
  grounding query, and a structured, auditable `Finding` contract.
- **A real confidence-weighted HITL gate**, not "comment on everything."
  Anything CRITICAL always escalates to a human queue; only high-confidence,
  non-critical findings auto-post — and the threshold is yours to see and
  tune, not a black box.
- **A hard cost cap that actually blocks spend.** `BudgetGuard` checks a live
  cost ledger before every LLM call and refuses the next one once your daily
  cap is hit — no surprise bill, because there's no bill at all beyond your
  own infra.
- **Prompt changes are version-pinned and regression-tested.** Every review
  traces to a content-hashed prompt version, and a golden-dataset eval gate
  blocks a prompt edit that silently degrades recall or adds false
  positives.
- **A documented threat model.** Untrusted PR content is treated as
  attacker-influenceable input: an injection guard delimits and flags
  prompt-injection attempts in diffs/retrieved code before they ever reach a
  specialist prompt.
- **Free.** No per-seat, no per-review credits — you pay only for your own
  Tiger Cloud + OpenAI usage.

## How is it better than CodeRabbit / Greptile / Qodo?

Those are SaaS products you pay per seat or per review, and your diffs flow
through their infrastructure. This is a self-hosted pipeline you run and own
end to end.

| | CodeRabbit / Greptile / Qodo | `ai-pr-review` |
|---|---|---|
| Where your code goes | Their SaaS backend | Only your own OpenAI account + your own DB |
| Pricing | Per-seat or per-review credits ($12–60+/mo) | Free — you pay only your own infra |
| Grounding | Diff-focused (Greptile also indexes repo) | Hybrid vector + FTS over the whole repo, always |
| Review structure | One general pass | 4 independently-reasoned specialists |
| Escalation logic | Tuned by the vendor, opaque | Confidence-weighted HITL gate, yours to see and tune |
| Cost control | Whatever they bill you | A hard daily cap that blocks the next LLM call |
| Prompt changes | Opaque, vendor-controlled | Content-hashed, version-pinned, regression-gated |
| Data ownership | Vendor's servers | Your own Postgres-compatible store |
| Multi-tenant / teams | Yes, built for orgs | No — single operator by design (see below) |

The honest tradeoff: this is built for **one operator** (single bearer-token
auth, no RBAC, no org-wide dashboard) — it trades multi-tenant SaaS polish
for full transparency and control over your own pipeline.

## Who is this for?

- **A solo developer or small team** who wants automated PR review across
  their own repos without sending code to a third-party SaaS.
- **Anyone who wants to see and control the cost** of running LLM-based
  review — the budget cap and cost ledger are first-class, not an
  afterthought.
- **Engineers who want to inspect or customize the review logic** — prompts,
  the confidence threshold, the grounding retrieval, the specialists
  themselves — all live in this repo, not behind a vendor's API.
- **Not** (yet) a fit for a multi-team organization that needs per-user
  permissions, SSO, or an admin console — that's explicitly on the
  [TODO](#todo) list, not shipped.

## What it can do / what it can't (yet)

**Can:**
- Review PRs automatically across any number of repos your GitHub App is
  installed on, with zero per-repo config.
- Ground every finding in the real surrounding codebase via hybrid
  vector + full-text retrieval, not just the diff.
- Post one aggregated review comment per PR (not scattered inline comments),
  or auto-approve when there's nothing to flag.
- Escalate uncertain or CRITICAL findings to a human queue you resolve from
  a dashboard, with idempotent approve/reject (never double-posts).
- Enforce a hard daily spend cap and show you exactly where the money went,
  per review, per agent, per LLM call.
- Survive and recover from transient failures — every outbound call (DB,
  LLM, GitHub, Redis) is retried, circuit-broken, and timeout-guarded, with
  a fault-injection test suite proving it.
- Guard against prompt-injection payloads hidden in a malicious PR's diff or
  file content.
- Catch prompt-quality regressions before they ship, via a golden-dataset
  eval gate wired into CI.

**Can't yet** (see [TODO](#todo) for the full list):
- Support multiple users/teams with different permissions — it's single
  bearer-token auth, one operator.
- Mask secrets that might appear in traced event payloads.
- Predictively (rather than reactively) throttle spend before it's incurred.
- Offer a prompt playground, a trace viewer UI, or drift detection over
  time — these are still on the roadmap.

## Install & run

### With Docker

Prerequisites: Docker, and a **Tiger Cloud / Postgres** database (the
compose stack does not run one — only Redis).

Redis is queue/cache only, never durable truth (ADR-0003) — the bundled
`redis:7-alpine` container (persistence disabled) is just for zero-setup
local use. To use a hosted Redis (e.g. Upstash) instead: set `REDIS_URL` in
`backend/.env` to its connection string, then in `docker-compose.yml` remove
the `redis:` service, the `REDIS_URL: redis://redis:6379` override in the
`x-backend` anchor (it currently overrides `.env`), and each service's
`depends_on: redis`.

1. **Configure.**
   ```bash
   cp backend/.env.example backend/.env
   # edit backend/.env: TIGER_DATABASE_URL, OPENAI_API_KEY, GITHUB_*, API_AUTH_TOKEN
   # (API_AUTH_TOKEN: generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
   # put your GitHub App private key in backend/secrets/  (gitignored)
   ```

2. **Migrate the database** (one time, and after a schema change — run in
   order, all idempotent/`IF NOT EXISTS`, safe to re-run):
   ```bash
   psql "$TIGER_DATABASE_URL" -f scripts/migrations/2026-06-tiger-init.sql
   psql "$TIGER_DATABASE_URL" -f scripts/migrations/2026-09-reliability.sql
   psql "$TIGER_DATABASE_URL" -f scripts/migrations/2026-09-ingestion.sql
   psql "$TIGER_DATABASE_URL" -f scripts/migrations/2026-09-hitl-resolution.sql
   ```

3. **Start.**
   ```bash
   docker compose up --build
   ```
   - webhook receiver → http://localhost:8000  (point the GitHub App webhook here)
   - read API         → http://localhost:8001
   - dashboard        → `docker compose --profile frontend up --build` → http://localhost:3000
     (paste `API_AUTH_TOKEN` on the `/login` page it redirects you to)

One image (`backend/Dockerfile`) runs all three backend processes; compose
picks which via each service's `command:`.

### Without Docker

```bash
cd backend
uv sync
export $(grep -v '^#' .env | xargs)          # or use direnv
uv run uvicorn webhook_receiver.app:build_default_app --factory --port 8000
uv run uvicorn api.app:build_default_app --factory --port 8001
uv run arq job_queue.arq_worker.WorkerSettings
```
Needs a local Redis (`REDIS_URL`, default `redis://localhost:6379`).

Frontend (separate terminal — bare-metal `next dev` picks up env vars live, no
build-arg dance needed):
```bash
cd frontend
npm install
NEXT_PUBLIC_BACKEND_URL=http://localhost:8001 npm run dev
```
Open http://localhost:3000 — it redirects to `/login`; paste the `API_AUTH_TOKEN`
from `backend/.env`.

To actually receive GitHub webhooks locally (not required just to browse the
dashboard against data already in Tiger Cloud), forward them with smee — see
`.genesis/checkpoints/CREDENTIALS.md` §4 for the one-time GitHub App + smee.io
setup, then:
```bash
npx smee-client --url <your smee.io URL> --path /webhook --port 8000
```

## Multiple repositories

There's no per-repo config or CLI flag — the set of repos this instance serves
is just whatever your GitHub App is installed on. One running instance
already supports any number of repos.

1. **Add a repo** — install the GitHub App on it, or (if already installed)
   edit the installation to add more repos.
2. That fires an `installation` (new install) or `installation_repositories`
   (repos added to an existing install) webhook, which `webhook_receiver/app.py`
   turns into an ingestion job per repo — `ingestion/indexer.py`'s `full_index`
   fetches, chunks, and embeds it, writing to `repo_index_state`/`code_chunks`
   keyed by `repo` (`owner/name`).
3. From then on, `pull_request` webhooks on that repo are reviewed
   automatically, and `push` webhooks keep its index incrementally updated
   (`incremental_index`) — nothing else to configure.
4. The dashboard's repo list (`GET /api/repos`) already aggregates across all
   indexed repos, showing each one's chunk and review counts.

## Develop / test

```bash
cd backend
uv run mypy .        # Definition-of-Done gate — clean
uv run pytest -q     # most tests are fake-driven and need no creds;
                      # tests hitting live Tiger Cloud/OpenAI skip cleanly
                      # without TIGER_DATABASE_URL / OPENAI_API_KEY set

cd ../frontend
npm run build         # type-checks + builds all routes
```

No live credentials or a running server needed for `mypy`/most of `pytest`/
`npm run build` — that's the fast inner loop. To exercise the full stack (real
DB reads/writes, a real dashboard you click through), run it per the sections
above with real `backend/.env` values.

CI runs both on every push/PR. `eval-gate.yml` runs the M9 regression gate on
PRs touching `backend/agents/**`, `backend/prompts/**`, or `backend/evaluation/**` —
soft (skips) without `TIGER_DATABASE_URL` / `OPENAI_API_KEY` repo secrets, hard
with them.

## Layout

| path        | what                                                              |
|-------------|------------------------------------------------------------------|
| `backend/`  | Python. Three processes: `webhook_receiver`, `api`, arq `worker`. |
| `frontend/` | Next.js dashboard (repos, reviews, HITL inbox). Optional.        |
| `scripts/migrations/` | Idempotent SQL for the Tiger Cloud schema.               |
| `.github/workflows/`  | `ci.yml` (mypy + pytest + build), `eval-gate.yml`.       |

## TODO

M1–M16 (data spine, webhook/queue, orchestration, grounded agents,
aggregator/HITL, observability/budget, frontend, security, eval, reliability,
CI/CD, multi-repo ingestion, auth, HITL API, dashboard) are done. Remaining,
tracked in the project's internal planning notes:

**Deferred roadmap phases (unsliced):**
- [ ] Governance / explainability
- [ ] DX: prompt playground, trace viewer
- [ ] Continuous learning / drift detection

**Carried-forward gaps:**
- [ ] Auth: RBAC / multi-user (currently single-user bearer token, no
      per-user audit identity, revocation is redeploy-only)
- [ ] Secret-masking in `agent_events` trace payloads
- [ ] Second-layer LLM classifier for prompt injection (currently
      regex/structural guard only)
- [ ] `code_chunks` ingestion-time provenance/signing; rate-limiting
- [ ] Concurrency hardening: `SELECT ... FOR UPDATE` in `upsert_review`,
      the `mark_posted` crash window, unbounded half-open probe concurrency
      in circuit breakers
- [ ] `Guard`-wrap the `api/` read endpoints (currently timeout-only)
- [ ] De-duplicate the circuit-breaker implementations in `job_queue` and
      `github_client` onto `reliability/`
- [ ] Eval: persist run history, grow the golden set past 3 cases, an eval
      dashboard, wire `regression_gate` into real CI secrets + a canary path
- [ ] Budget: predictive (not just post-hoc) blocking; a cost rollup/cache
      for `BudgetGuard` at high throughput
- [ ] Observability: OTel export, alerting
- [ ] A real chaos harness against live Tiger Cloud/Redis; exercise the
      `arq` worker in CI
- [ ] Refresh `context-graph.json` (`graphizer.mjs`) — stale since M13

**Open-source housekeeping (not blocking):**
- [ ] GitHub issue templates (bug report + feature request)
- [ ] Set repo topics/description on GitHub to match this README's tagline

## Prompt versioning

Specialist system prompts live in `backend/prompts/*.md` and are content-hashed
by `prompts/registry.py` (`sha256[:12]`). The hash is stamped onto each review's
`span.start` event, so a stored review traces to the exact prompt that produced
it. When you edit a prompt, add a row to `backend/prompts/REGISTRY.md` —
`tests/test_prompt_registry.py` (and CI) fail until it matches.
