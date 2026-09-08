-- M10 (Reliability under fault injection) — idempotency of the truth-lane write.
--
-- Before this, a retried TruthStore.insert_findings (a network blip mid-write,
-- or a re-run of a crashed aggregate) double-inserted every finding: there was
-- no key to conflict on. This unique index gives insert_findings an
-- ON CONFLICT DO NOTHING target, so the write becomes idempotent.
--
-- COALESCE(line_start, -1): a NULL line_start is common (finding with no
-- specific line) and NULLs are distinct in a plain unique index, which would
-- let duplicates through. -1 is not a valid 1-indexed line, so it is a safe
-- sentinel.
--
-- Idempotent (IF NOT EXISTS) — safe to re-run, same discipline as
-- 2026-06-tiger-init.sql.

CREATE UNIQUE INDEX IF NOT EXISTS finding_records_dedup_idx
    ON finding_records (review_id, agent_type, file_path, COALESCE(line_start, -1), category);
