-- M15: HITL resolution. `hitl_reviews` already had `status`/`resolved_at`
-- (provisioned by M1's 2026-06-tiger-init.sql, unused until now) — the only
-- gap is a place to record why a human resolved an item.
ALTER TABLE hitl_reviews ADD COLUMN IF NOT EXISTS resolution_note TEXT;
