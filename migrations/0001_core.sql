-- Core schema (docs §P.2). Never edit an applied migration — add a new one.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE events (
    id              BIGSERIAL PRIMARY KEY,
    external_id     TEXT NOT NULL UNIQUE, -- the dedupe boundary
    type            TEXT NOT NULL,
    mandate_id      TEXT NOT NULL,
    cycle_id        TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL,
    sequence_hint   INT
);

CREATE TABLE mandates (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL,
    payer_id        TEXT NOT NULL,
    rail            TEXT NOT NULL CHECK (rail IN ('upi_autopay','enach')),
    max_amount      NUMERIC(12,2) NOT NULL,
    status          TEXT NOT NULL
                        CHECK (status IN ('active','paused','revoked','expired')),
    opted_out       BOOLEAN NOT NULL DEFAULT false,
    issuer_code     TEXT NOT NULL,
    version         INT NOT NULL DEFAULT 0 -- optimistic locking
);

CREATE TABLE cycles (
    id                  TEXT PRIMARY KEY,
    mandate_id          TEXT NOT NULL REFERENCES mandates(id),
    due_date            DATE NOT NULL,
    amount              NUMERIC(12,2) NOT NULL,
    state               TEXT NOT NULL,
    attempts_used       SMALLINT NOT NULL DEFAULT 0
                            CHECK (attempts_used BETWEEN 0 AND 4),
    recovered_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
    closed_at           TIMESTAMPTZ,
    UNIQUE (mandate_id, due_date)
);

-- THE constraint. A database invariant, not application logic.
CREATE TABLE attempt_intents (
    id                  BIGSERIAL PRIMARY KEY,
    cycle_id            TEXT NOT NULL REFERENCES cycles(id),
    sequence_no         SMALLINT NOT NULL CHECK (sequence_no BETWEEN 1 AND 4),
    idempotency_key     TEXT NOT NULL UNIQUE,
    scheduled_for       TIMESTAMPTZ NOT NULL,
    executed_at         TIMESTAMPTZ,
    outcome             TEXT, -- success | failure | blocked | unknown
    raw_reason          TEXT,
    canonical_cause     TEXT,
    cause_confidence    REAL,
    cause_source        TEXT, -- dictionary | llm | unknown
    plan_id             BIGINT,
    UNIQUE (cycle_id, sequence_no) -- four attempts, enforced by Postgres
);

CREATE TABLE plans (
    id                  BIGSERIAL PRIMARY KEY,
    cycle_id            TEXT NOT NULL REFERENCES cycles(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by       BIGINT REFERENCES plans(id),
    model_version       TEXT NOT NULL,
    feature_hash        TEXT NOT NULL,
    expected_value      NUMERIC(12,2) NOT NULL,
    stop_reason         TEXT,
    solver_ms           INT NOT NULL
);

CREATE TABLE plan_steps (
    id                  BIGSERIAL PRIMARY KEY,
    plan_id             BIGINT NOT NULL REFERENCES plans(id),
    step_type           TEXT NOT NULL, -- notify | attempt | escalate
    scheduled_for       TIMESTAMPTZ NOT NULL,
    p_success           REAL,
    status              TEXT NOT NULL DEFAULT 'pending',
    cancelled_reason    TEXT
);

CREATE TABLE notifications (
    id                  BIGSERIAL PRIMARY KEY,
    cycle_id            TEXT NOT NULL REFERENCES cycles(id),
    sent_at             TIMESTAMPTZ NOT NULL,
    covers_debit_at     TIMESTAMPTZ NOT NULL, -- the RBI 24h linkage
    channel             TEXT NOT NULL,
    body                TEXT NOT NULL,
    generated_by        TEXT NOT NULL, -- llm | template
    validator_result    JSONB NOT NULL
);

CREATE TABLE decisions ( -- every authorize(), both verdicts
    id                  BIGSERIAL PRIMARY KEY,
    at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    cycle_id            TEXT NOT NULL,
    action              TEXT NOT NULL,
    verdict             TEXT NOT NULL, -- allow | deny
    reason_code         TEXT,
    policy_version      TEXT NOT NULL,
    input_snapshot      JSONB NOT NULL
);

CREATE TABLE audit_ledger (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL, -- system | operator:<id>
    cycle_id    TEXT,
    action      TEXT NOT NULL,
    detail      JSONB NOT NULL,
    prev_hash   TEXT,
    hash        TEXT NOT NULL -- tamper-evident chain
);

CREATE TABLE outbox (
    id                  BIGSERIAL PRIMARY KEY,
    destination         TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL UNIQUE,
    payload             JSONB NOT NULL,
    attempts            INT NOT NULL DEFAULT 0,
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at        TIMESTAMPTZ
);

CREATE INDEX plan_steps_pending_idx ON plan_steps (status, scheduled_for)
    WHERE status = 'pending';
CREATE INDEX outbox_undelivered_idx ON outbox (next_attempt_at)
    WHERE delivered_at IS NULL;
CREATE INDEX cycles_open_state_idx ON cycles (state)
    WHERE state NOT IN ('RECOVERED', 'ABANDONED');

-- Tamper-evident hash chain: each row's hash covers its own fields plus the
-- previous row's hash, so any edit to history breaks every hash after it.
CREATE OR REPLACE FUNCTION audit_ledger_chain() RETURNS TRIGGER AS $$
DECLARE
    prev TEXT;
BEGIN
    SELECT hash INTO prev FROM audit_ledger ORDER BY id DESC LIMIT 1;
    NEW.prev_hash := prev;
    NEW.hash := encode(
        digest(
            coalesce(prev, '') || NEW.actor || coalesce(NEW.cycle_id, '') ||
            NEW.action || NEW.detail::text || NEW.at::text,
            'sha256'
        ),
        'hex'
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_ledger_chain_trigger
    BEFORE INSERT ON audit_ledger
    FOR EACH ROW
    EXECUTE FUNCTION audit_ledger_chain();
