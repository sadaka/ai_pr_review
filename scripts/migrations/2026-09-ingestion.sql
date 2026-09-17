-- 2026-09-ingestion.sql
-- M13 (Multi-repo codebase ingestion & indexing pipeline).
--
-- `code_chunks` and `repo_file_index` already exist from 2026-06-tiger-init.sql
-- (M1 provisioned the memory lane before M13 built its writer). This adds the
-- one new table M13 needs: per-repo indexing state (which commit was last
-- fully/incrementally indexed, and how many chunks that produced) — distinct
-- from `repo_file_index`'s per-file content-hash freshness tracking, which
-- M13 does not use (the push webhook's own added/modified/removed file lists
-- are the freshness signal for incremental re-index, per ADR-0005).
--
-- Idempotent (IF NOT EXISTS) — safe to re-run, same discipline as the other
-- migrations in this directory.

CREATE TABLE IF NOT EXISTS repo_index_state (
    repo                 TEXT         PRIMARY KEY,
    last_indexed_commit  TEXT         NOT NULL,
    indexed_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    chunk_count          INT          NOT NULL DEFAULT 0
);
