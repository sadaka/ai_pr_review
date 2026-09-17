# Contributing

Thanks for considering a contribution. This is a small, single-maintainer
project, so keeping changes tightly scoped and passing the checks below
before opening a PR will save both of us review round-trips.

## Setup

```bash
cd backend
uv sync                 # installs backend deps from uv.lock
cp .env.example .env    # fill in credentials — see the README's
                         # "Install & run" section for where to get each one
```

```bash
cd frontend
npm install
```

## Before opening a PR

Run these from `backend/`:

```bash
mypy .
pytest -q
```

Both must be clean. Tests that require live credentials (Tiger Cloud,
OpenAI, a GitHub App) are marked to skip cleanly without them — you don't
need every credential to contribute to most of the codebase.

From `frontend/`:

```bash
npm run build
```

## Prompt changes

If you edit anything under `backend/prompts/*.md`, you must add a row to
`backend/prompts/REGISTRY.md` with the prompt's new content hash and a
one-line note — `tests/test_prompt_registry.py` enforces this and will fail
otherwise. See the file for the exact format.

## Code conventions

- Follow the existing dependency direction: leaf modules (`reliability/`,
  `evaluation/`) don't import back into the app; cross-cutting modules
  (`observability/`, `security/`) are the only declared exceptions. If your
  change needs a new cross-module import, prefer Protocol-based injection
  (see `orchestrator/nodes.py` for the pattern) over widening the boundary.
- New Pydantic models that cross a process/checkpoint boundary should follow
  the `structured_output_only` convention already used throughout
  (`agents/contracts.py`'s `Finding` is the reference example).
- Don't wire new outbound calls (DB, LLM, GitHub, Redis) without a timeout +
  retry/circuit-breaker via `backend/reliability/` — see `guard.py`.

## Pull requests

- Keep PRs focused on one change. If you find something unrelated that needs
  fixing, open a separate issue/PR.
- Add or update tests for behavior you change. A bug fix without a
  regression test is not considered done.
- Describe *why* the change is needed, not just what it does — the "why"
  belongs in the PR description since it doesn't survive in the code itself.

## Reporting security issues

Please don't file a public issue for a vulnerability — see
[`SECURITY.md`](SECURITY.md) for how to report privately.
