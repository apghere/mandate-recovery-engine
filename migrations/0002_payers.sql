-- Payer population, persisted (docs §P.2 predates this table -- payer
-- attributes previously only existed transiently via
-- data.generator.generate_population()). Needed once real cycles need
-- real payer context (credit_day, mean_balance, ...) to score planner
-- slots -- see docs/DATA_MODEL.md for what each column means and how
-- it's generated. No FK from mandates.payer_id: CLAUDE.md forbids editing
-- 0001_core.sql, so the link stays an unenforced application-level
-- convention rather than forcing a retrofit ALTER TABLE.

CREATE TABLE payers (
    id                          TEXT PRIMARY KEY,
    segment                     TEXT NOT NULL,
    credit_day                  SMALLINT NOT NULL CHECK (credit_day BETWEEN 1 AND 28),
    mean_balance                NUMERIC(12,2) NOT NULL,
    balance_volatility          DOUBLE PRECISION NOT NULL,
    issuer_code                 TEXT NOT NULL,
    chronic_fail_propensity     DOUBLE PRECISION NOT NULL,
    annoyance_sensitivity       DOUBLE PRECISION NOT NULL,
    mandate_amount              NUMERIC(12,2) NOT NULL,
    split                       TEXT NOT NULL
);

CREATE INDEX payers_split_idx ON payers (split);
