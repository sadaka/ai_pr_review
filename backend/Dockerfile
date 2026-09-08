# One image, three processes. `docker-compose.yml` picks which one each service
# runs via its `command:` — webhook receiver, read API, or the arq worker.
FROM python:3.12-slim AS base

# uv: fast, lockfile-driven installs. Pinned for reproducible builds.
COPY --from=ghcr.io/astral-sh/uv:0.8.5 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached until the lockfile changes), then the app.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Non-root runtime.
RUN useradd --system --uid 1001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Default process is the webhook receiver; compose overrides for api / worker.
CMD ["uvicorn", "webhook_receiver.app:build_default_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
