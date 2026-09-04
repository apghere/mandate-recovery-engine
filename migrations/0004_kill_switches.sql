-- Phase 9 (docs FR-12 / Day 6): the dashboard's audit screen needs a real
-- kill switch to display and flip, not the hardcoded `False` worker.py's
-- _build_snapshot has used since Phase 3. Two independent scopes, matching
-- domain/policy.py's existing authorize() checks (DenyReason.
-- GLOBAL_KILL_SWITCH / MERCHANT_KILL_SWITCH) -- one global row, and any
-- number of per-merchant rows. "There is no override path through the
-- policy engine -- operator overrides are separate actions that are
-- themselves authorised" (docs I.10): flipping a switch here is itself
-- recorded to audit_ledger by the caller, not by a trigger on this table.

CREATE TABLE kill_switches (
    scope       TEXT PRIMARY KEY,   -- 'global' or 'merchant:<merchant_id>'
    active      BOOLEAN NOT NULL DEFAULT false,
    set_by      TEXT NOT NULL,
    set_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
