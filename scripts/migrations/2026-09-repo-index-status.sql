-- 2026-09-repo-index-status.sql
--
-- `repo_index_state` (2026-09-ingestion.sql) only ever gets a row written by
-- the indexer's write path on a *successful* full/incremental index — a repo
-- that was just added via the GitHub App has no row at all until that first
-- index finishes, and a job that errors mid-flight never touches the table.
-- That leaves the dashboard with no way to show "we got the webhook, indexing
-- is in progress" or "indexing failed" for a repo.
--
-- This adds a `status` column so the webhook receiver can upsert a `pending`
-- row as soon as it enqueues an index/reindex job, the worker can flip it to
-- `done` on success or `failed` (with `error`) on exception, and the read API
-- can surface it. `last_indexed_commit` is relaxed to nullable since a
-- `pending` row (no successful index yet) has no commit to report.
--
-- Idempotent (IF NOT EXISTS / guarded DO block) — safe to re-run, same
-- discipline as the other migrations in this directory.

ALTER TABLE repo_index_state
    ALTER COLUMN last_indexed_commit DROP NOT NULL;

ALTER TABLE repo_index_state
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'done',
    ADD COLUMN IF NOT EXISTS error  TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'repo_index_state_status_check'
    ) THEN
        ALTER TABLE repo_index_state
            ADD CONSTRAINT repo_index_state_status_check
            CHECK (status IN ('pending', 'done', 'failed'));
    END IF;
END $$;
