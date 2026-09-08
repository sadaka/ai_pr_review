-- 2026-06-tiger-init.sql
-- Idempotent schema DDL for the one Tiger Cloud data spine (ADR-003).
-- Three lanes in one Postgres-compatible database: memory (code_chunks),
-- time (agent_events + continuous aggregates), truth (relational review tables).
-- Safe to re-run: every statement is IF NOT EXISTS / CREATE OR REPLACE.

-- ── Extensions ───────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vectorscale;

-- ── Lane 1: Memory — code_chunks ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS code_chunks (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    repo         TEXT         NOT NULL,
    path         TEXT         NOT NULL,
    symbol       TEXT,
    chunk_index  INT          NOT NULL,
    content      TEXT         NOT NULL,
    embedding    VECTOR(256)  NOT NULL,
    token_count  INT,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS code_chunks_unique_idx
    ON code_chunks (repo, path, chunk_index);

CREATE INDEX IF NOT EXISTS code_chunks_emb_idx
    ON code_chunks USING diskann (embedding vector_cosine_ops);

ALTER TABLE code_chunks
    ADD COLUMN IF NOT EXISTS content_tsv TSVECTOR
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS code_chunks_fts_idx
    ON code_chunks USING GIN (content_tsv);

-- Freshness tracking so ingestion only re-embeds files that changed.
CREATE TABLE IF NOT EXISTS repo_file_index (
    repo             TEXT         NOT NULL,
    path             TEXT         NOT NULL,
    content_hash     TEXT         NOT NULL,
    last_indexed_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, path)
);

-- ── Lane 2: Time — agent_events hypertable ──────────────────────────────
CREATE TABLE IF NOT EXISTS agent_events (
    ts            TIMESTAMPTZ  NOT NULL,
    review_id     UUID         NOT NULL,
    agent         TEXT         NOT NULL,
    span_id       UUID         NOT NULL DEFAULT gen_random_uuid(),
    parent_span   UUID,
    event_type    TEXT         NOT NULL,
    model         TEXT,
    tokens_in     INT,
    tokens_out    INT,
    cost_usd      NUMERIC(10,6),
    latency_ms    INT,
    outcome       TEXT,
    confidence    NUMERIC(4,3),
    payload       JSONB
);

SELECT create_hypertable(
    'agent_events',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS agent_events_review_idx
    ON agent_events (review_id, ts);

-- ── Lane 3: Rollups — continuous aggregates ─────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts)                          AS bucket,
    agent,
    count(*) FILTER (WHERE event_type = 'llm.call')      AS llm_calls,
    sum(cost_usd)                                        AS cost_usd,
    approx_percentile(0.95, percentile_agg(latency_ms))  AS p95_ms,
    count(*) FILTER (WHERE outcome = 'rejected')::float
        / NULLIF(count(*) FILTER (WHERE outcome IS NOT NULL), 0) AS rejection_rate
FROM agent_events
GROUP BY bucket, agent
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'agent_health_1m',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);

CREATE MATERIALIZED VIEW IF NOT EXISTS pr_cost_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts)   AS bucket,
    review_id,
    sum(cost_usd)               AS total_cost_usd,
    count(DISTINCT agent)       AS agents_used,
    max(confidence)             AS max_confidence
FROM agent_events
GROUP BY bucket, review_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'pr_cost_hourly',
    start_offset      => INTERVAL '1 day',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);

-- ── Truth lane — ordinary relational tables (M2/M5 write to these) ─────
CREATE TABLE IF NOT EXISTS pr_review_records (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    repo              TEXT         NOT NULL,
    pr_number         INT          NOT NULL,
    github_delivery_id TEXT        NOT NULL,
    status            TEXT         NOT NULL DEFAULT 'pending',
    overall_confidence NUMERIC(4,3),
    github_review_id  BIGINT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS pr_review_records_delivery_idx
    ON pr_review_records (github_delivery_id);

CREATE TABLE IF NOT EXISTS finding_records (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id     UUID         NOT NULL REFERENCES pr_review_records(id),
    agent_type    TEXT         NOT NULL,
    severity      TEXT         NOT NULL,
    category      TEXT         NOT NULL,
    summary       TEXT         NOT NULL,
    file_path     TEXT         NOT NULL,
    line_start    INT,
    line_end      INT,
    suggestion    TEXT,
    confidence    NUMERIC(4,3) NOT NULL,
    rationale     TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Makes TruthStore.insert_findings idempotent (ON CONFLICT DO NOTHING target)
-- so a retried or replayed write inserts each finding once. Added in the
-- 2026-09-reliability.sql migration (M10); folded here for fresh provisions.
CREATE UNIQUE INDEX IF NOT EXISTS finding_records_dedup_idx
    ON finding_records (review_id, agent_type, file_path, COALESCE(line_start, -1), category);

CREATE TABLE IF NOT EXISTS hitl_reviews (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id     UUID         NOT NULL REFERENCES pr_review_records(id),
    reason        TEXT         NOT NULL,
    status        TEXT         NOT NULL DEFAULT 'pending',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS hitl_feedback (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id    UUID         NOT NULL REFERENCES finding_records(id),
    disputed_by   TEXT         NOT NULL,
    note          TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
