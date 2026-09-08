# ai-pr-review

Agentic pull-request review. A GitHub webhook fans a PR diff out to four
grounded specialist agents (security, quality, tests, docs), aggregates and
confidence-gates their findings, and either posts a review or routes it to a
human. Runs, costs, and traces land in a single Tiger Cloud (Postgres) data
spine.

```
GitHub ──webhook──▶ webhook_receiver ──enqueue──▶ redis ──▶ worker
                                                             │  (LangGraph: 4 specialists → aggregate)
                          frontend ◀──GET /api/reviews── api ◀┘
                                       │
                                  Tiger Cloud  (memory · events · truth)
```

## Layout

| path        | what                                                              |
|-------------|------------------------------------------------------------------|
| `backend/`  | Python. Three processes: `webhook_receiver`, `api`, arq `worker`. |
| `frontend/` | Next.js status page (M7). Optional.                               |
| `scripts/migrations/` | Idempotent SQL for the Tiger Cloud schema.               |
| `.github/workflows/`  | `ci.yml` (mypy + pytest + build), `eval-gate.yml`.       |

## Run it with Docker

Prerequisites: Docker, and a **Tiger Cloud / Postgres** database (the compose
stack does not run one — only Redis).

1. **Configure.**
   ```bash
   cp backend/.env.example backend/.env
   # edit backend/.env: TIGER_DATABASE_URL, OPENAI_API_KEY, GITHUB_* 
   # put your GitHub App private key in backend/secrets/  (gitignored)
   ```

2. **Migrate the database** (one time, and after a schema change):
   ```bash
   psql "$TIGER_DATABASE_URL" -f scripts/migrations/2026-06-tiger-init.sql
   psql "$TIGER_DATABASE_URL" -f scripts/migrations/2026-09-reliability.sql
   ```

3. **Start.**
   ```bash
   docker compose up --build
   ```
   - webhook receiver → http://localhost:8000  (point the GitHub App webhook here)
   - read API         → http://localhost:8001
   - status page      → `docker compose --profile frontend up` → http://localhost:3000

One image (`backend/Dockerfile`) runs all three backend processes; compose
picks which via each service's `command:`.

## Run it without Docker

```bash
cd backend
uv sync
export $(grep -v '^#' .env | xargs)          # or use direnv
uv run uvicorn webhook_receiver.app:build_default_app --factory --port 8000
uv run uvicorn api.app:build_default_app --factory --port 8001
uv run arq job_queue.arq_worker.WorkerSettings
```
Needs a local Redis (`REDIS_URL`, default `redis://localhost:6379`).

## Develop

```bash
cd backend
uv run mypy .        # Definition-of-Done gate — clean
uv run pytest -q     # 92 tests; live-credential tests skip without secrets
```

CI runs both on every push/PR. `eval-gate.yml` runs the M9 regression gate on
PRs touching `backend/agents/**`, `backend/prompts/**`, or `backend/evaluation/**` —
soft (skips) without `TIGER_DATABASE_URL` / `OPENAI_API_KEY` repo secrets, hard
with them.

### Prompt versioning

Specialist system prompts live in `backend/prompts/*.md` and are content-hashed
by `prompts/registry.py` (`sha256[:12]`). The hash is stamped onto each review's
`span.start` event, so a stored review traces to the exact prompt that produced
it. When you edit a prompt, add a row to `backend/prompts/REGISTRY.md` —
`tests/test_prompt_registry.py` (and CI) fail until it matches.
