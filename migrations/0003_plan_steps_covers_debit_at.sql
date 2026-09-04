-- A notify step must record which specific future attempt it covers,
-- rather than the worker assuming a fixed day-offset (docs I.10's
-- notice-freshness check is an *exact* covers_debit_at match). The fixed
-- baseline always pairs notify with an attempt exactly 1 day later, so
-- this went unnoticed until MRE's DP -- free to notify early and wait for
-- a better slot -- exposed it: every one of its scheduled attempts was
-- being denied RBI_NOTICE_NOT_SATISFIED because the worker assumed the
-- wrong covers_debit_at. See CLAUDE.md's Phase 5 status for the full story.

ALTER TABLE plan_steps ADD COLUMN covers_debit_at TIMESTAMPTZ;
