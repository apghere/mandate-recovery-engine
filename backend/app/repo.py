"""Database access. Every correctness-critical invariant enforced here is
actually enforced by Postgres (UNIQUE/CHECK/trigger) — these functions
apply the SQL, they don't reimplement the invariant in Python.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from psycopg.types.json import Json

from app.db import Conn


def upsert_payer(
    conn: Conn,
    *,
    payer_id: str,
    segment: str,
    credit_day: int,
    mean_balance: float,
    balance_volatility: float,
    issuer_code: str,
    chronic_fail_propensity: float,
    annoyance_sensitivity: float,
    mandate_amount: float,
    split: str,
) -> None:
    conn.execute(
        """
        INSERT INTO payers (id, segment, credit_day, mean_balance, balance_volatility,
                             issuer_code, chronic_fail_propensity, annoyance_sensitivity,
                             mandate_amount, split)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            segment = EXCLUDED.segment,
            credit_day = EXCLUDED.credit_day,
            mean_balance = EXCLUDED.mean_balance,
            balance_volatility = EXCLUDED.balance_volatility,
            issuer_code = EXCLUDED.issuer_code,
            chronic_fail_propensity = EXCLUDED.chronic_fail_propensity,
            annoyance_sensitivity = EXCLUDED.annoyance_sensitivity,
            mandate_amount = EXCLUDED.mandate_amount,
            split = EXCLUDED.split
        """,
        (
            payer_id, segment, credit_day, mean_balance, balance_volatility, issuer_code,
            chronic_fail_propensity, annoyance_sensitivity, mandate_amount, split,
        ),
    )


def get_payer(conn: Conn, payer_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM payers WHERE id = %s", (payer_id,)).fetchone()
    return dict(row) if row is not None else None


def insert_event(
    conn: Conn,
    *,
    external_id: str,
    event_type: str,
    mandate_id: str,
    cycle_id: str | None,
    occurred_at: datetime,
    payload: dict[str, Any],
) -> bool:
    """Returns False if external_id already existed (idempotent no-op)."""
    row = conn.execute(
        """
        INSERT INTO events (external_id, type, mandate_id, cycle_id, occurred_at, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (external_id) DO NOTHING
        RETURNING id
        """,
        (external_id, event_type, mandate_id, cycle_id, occurred_at, Json(payload)),
    ).fetchone()
    return row is not None


def upsert_mandate(
    conn: Conn,
    *,
    mandate_id: str,
    merchant_id: str,
    payer_id: str,
    rail: str,
    max_amount: float,
    status: str,
    issuer_code: str,
) -> None:
    conn.execute(
        """
        INSERT INTO mandates (id, merchant_id, payer_id, rail, max_amount, status, issuer_code)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status
        """,
        (mandate_id, merchant_id, payer_id, rail, max_amount, status, issuer_code),
    )


def create_cycle(
    conn: Conn,
    *,
    cycle_id: str,
    mandate_id: str,
    due_date: date,
    amount: float,
    state: str,
) -> bool:
    """Returns False if a cycle already existed for (mandate_id, due_date)."""
    row = conn.execute(
        """
        INSERT INTO cycles (id, mandate_id, due_date, amount, state)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (mandate_id, due_date) DO NOTHING
        RETURNING id
        """,
        (cycle_id, mandate_id, due_date, amount, state),
    ).fetchone()
    return row is not None


def get_cycle(conn: Conn, cycle_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM cycles WHERE id = %s", (cycle_id,)).fetchone()
    return dict(row) if row is not None else None


def get_mandate(conn: Conn, mandate_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM mandates WHERE id = %s", (mandate_id,)).fetchone()
    return dict(row) if row is not None else None


def update_cycle_state(
    conn: Conn,
    cycle_id: str,
    *,
    state: str,
    attempts_used: int | None = None,
    recovered_amount: float | None = None,
    closed_at: datetime | None = None,
) -> None:
    conn.execute(
        """
        UPDATE cycles SET
            state = %s,
            attempts_used = COALESCE(%s, attempts_used),
            recovered_amount = COALESCE(%s, recovered_amount),
            closed_at = COALESCE(%s, closed_at)
        WHERE id = %s
        """,
        (state, attempts_used, recovered_amount, closed_at, cycle_id),
    )


def insert_plan(
    conn: Conn,
    *,
    cycle_id: str,
    model_version: str,
    feature_hash: str,
    expected_value: float,
    stop_reason: str | None,
    solver_ms: int,
) -> int:
    row = conn.execute(
        """
        INSERT INTO plans (cycle_id, model_version, feature_hash, expected_value,
                            stop_reason, solver_ms)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (cycle_id, model_version, feature_hash, expected_value, stop_reason, solver_ms),
    ).fetchone()
    assert row is not None
    plan_id: int = row["id"]
    return plan_id


def insert_plan_step(
    conn: Conn,
    *,
    plan_id: int,
    step_type: str,
    scheduled_for: datetime,
    p_success: float | None = None,
    covers_debit_at: datetime | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO plan_steps (plan_id, step_type, scheduled_for, p_success, covers_debit_at)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (plan_id, step_type, scheduled_for, p_success, covers_debit_at),
    ).fetchone()
    assert row is not None
    step_id: int = row["id"]
    return step_id


def fetch_due_plan_steps(conn: Conn, *, now: datetime, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ps.*, p.cycle_id
        FROM plan_steps ps
        JOIN plans p ON p.id = ps.plan_id
        WHERE ps.status = 'pending' AND ps.scheduled_for <= %s
        ORDER BY ps.scheduled_for
        LIMIT %s
        FOR UPDATE OF ps SKIP LOCKED
        """,
        (now, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_plan_step(
    conn: Conn, step_id: int, *, status: str, cancelled_reason: str | None = None
) -> None:
    conn.execute(
        "UPDATE plan_steps SET status = %s, cancelled_reason = %s WHERE id = %s",
        (status, cancelled_reason, step_id),
    )


def scheduled_cycles_with_no_remaining_steps(conn: Conn) -> list[dict[str, Any]]:
    """Cycles stuck in SCHEDULED whose plan has nothing left pending or
    dispatched — e.g. every remaining attempt was denied at the policy gate
    (AFA_REQUIRED_NOT_SATISFIED with no consent flow modelled yet) rather
    than consumed, so a real BUDGET_EXHAUSTED via a 4th attempt never
    fires. Without sweeping these, such a cycle never reaches a terminal
    state at all."""
    rows = conn.execute(
        """
        SELECT c.id, c.attempts_used
        FROM cycles c
        WHERE c.state = 'SCHEDULED'
          AND NOT EXISTS (
              SELECT 1 FROM plan_steps ps
              JOIN plans p ON p.id = ps.plan_id
              WHERE p.cycle_id = c.id AND ps.status IN ('pending', 'dispatched')
          )
        """
    ).fetchall()
    return [dict(r) for r in rows]


def cancel_pending_steps_for_cycle(conn: Conn, cycle_id: str, *, reason: str) -> int:
    result = conn.execute(
        """
        UPDATE plan_steps ps SET status = 'cancelled', cancelled_reason = %s
        FROM plans p
        WHERE ps.plan_id = p.id AND p.cycle_id = %s AND ps.status = 'pending'
        """,
        (reason, cycle_id),
    )
    return result.rowcount


def reserve_attempt_intent(
    conn: Conn,
    *,
    cycle_id: str,
    sequence_no: int,
    idempotency_key: str,
    scheduled_for: datetime,
) -> int:
    """Raises psycopg.errors.UniqueViolation if the (cycle_id, sequence_no)
    cap is already saturated — the DB constraint, not this function, is what
    enforces the four-attempt invariant."""
    row = conn.execute(
        """
        INSERT INTO attempt_intents (cycle_id, sequence_no, idempotency_key, scheduled_for)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (cycle_id, sequence_no, idempotency_key, scheduled_for),
    ).fetchone()
    assert row is not None
    intent_id: int = row["id"]
    return intent_id


def count_attempts(conn: Conn, cycle_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM attempt_intents WHERE cycle_id = %s", (cycle_id,)
    ).fetchone()
    assert row is not None
    n: int = row["n"]
    return n


def update_attempt_outcome(
    conn: Conn,
    intent_id: int,
    *,
    outcome: str,
    raw_reason: str | None,
    executed_at: datetime,
) -> None:
    conn.execute(
        """
        UPDATE attempt_intents
        SET outcome = %s, raw_reason = %s, executed_at = %s
        WHERE id = %s
        """,
        (outcome, raw_reason, executed_at, intent_id),
    )


def insert_outbox(
    conn: Conn, *, destination: str, idempotency_key: str, payload: dict[str, Any]
) -> int:
    row = conn.execute(
        """
        INSERT INTO outbox (destination, idempotency_key, payload)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (destination, idempotency_key, Json(payload)),
    ).fetchone()
    assert row is not None
    outbox_id: int = row["id"]
    return outbox_id


def fetch_pending_outbox(conn: Conn, *, now: datetime, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM outbox
        WHERE delivered_at IS NULL AND next_attempt_at <= %s
        ORDER BY next_attempt_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (now, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_outbox_delivered(conn: Conn, outbox_id: int, *, delivered_at: datetime) -> None:
    conn.execute("UPDATE outbox SET delivered_at = %s WHERE id = %s", (delivered_at, outbox_id))


def bump_outbox_attempt(conn: Conn, outbox_id: int, *, next_attempt_at: datetime) -> None:
    conn.execute(
        "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = %s WHERE id = %s",
        (next_attempt_at, outbox_id),
    )


def insert_decision(
    conn: Conn,
    *,
    cycle_id: str,
    action: str,
    verdict: str,
    reason_code: str | None,
    policy_version: str,
    input_snapshot: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO decisions (cycle_id, action, verdict, reason_code, policy_version,
                                input_snapshot)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (cycle_id, action, verdict, reason_code, policy_version, Json(input_snapshot)),
    )


def insert_audit(
    conn: Conn, *, actor: str, cycle_id: str | None, action: str, detail: dict[str, Any]
) -> None:
    # prev_hash/hash are computed by the audit_ledger_chain trigger.
    conn.execute(
        "INSERT INTO audit_ledger (actor, cycle_id, action, detail) VALUES (%s, %s, %s, %s)",
        (actor, cycle_id, action, Json(detail)),
    )


def insert_notification(
    conn: Conn,
    *,
    cycle_id: str,
    sent_at: datetime,
    covers_debit_at: datetime,
    channel: str,
    body: str,
    generated_by: str,
    validator_result: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO notifications (cycle_id, sent_at, covers_debit_at, channel, body,
                                    generated_by, validator_result)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (cycle_id, sent_at, covers_debit_at, channel, body, generated_by, Json(validator_result)),
    )


def notices_covering(conn: Conn, cycle_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT sent_at, covers_debit_at FROM notifications WHERE cycle_id = %s", (cycle_id,)
    ).fetchall()
    return [dict(r) for r in rows]
