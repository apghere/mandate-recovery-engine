"""Phase 7 chaos suite (docs §M.1's failure matrix): out-of-order event
delivery and mid-plan mandate lifecycle events, exercised against the real
ingestion + worker pipeline over live Postgres — not mocked, and not
merely unit-tested in isolation, since the whole point of this suite is
catching interactions between ingest.py and worker.py that a component
test can't see.

Duplicate delivery and delayed-but-in-order delivery are already covered
elsewhere (test_worker_pipeline.py::test_cycle_due_ingestion_is_idempotent
and every other ingest_* call in this file that's re-postable by
construction via ON CONFLICT DO NOTHING on events.external_id) — this file
adds the two failure modes that weren't exercised yet: arrival *before*
the events they causally depend on, and mandate-level lifecycle events
arriving mid-plan.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from app import repo
from app.adapters.simulator_client import SimulatorClient
from app.db import Conn
from app.ingest import (
    CycleDueEvent,
    DebitOutcomeEvent,
    MandateLifecycleEvent,
    UnknownCycleError,
    ingest_cycle_due,
    ingest_debit_failed,
    ingest_debit_succeeded,
    ingest_mandate_revoked,
    ingest_notification_opted_out,
)
from app.workflows.worker import drain_outbox, process_due_plan_steps

DUE_DATE = date(2026, 9, 1)
DUE_AT = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)


def _seed_mandate_and_cycle(
    conn: Conn, cycle_id: str, mandate_id: str = "M1", amount: float = 500.0
) -> None:
    result = ingest_cycle_due(
        conn,
        CycleDueEvent(
            external_id=f"ext:{cycle_id}:due",
            mandate_id=mandate_id,
            cycle_id=cycle_id,
            merchant_id="MERCH1",
            payer_id="PAYER1",
            rail="upi_autopay",
            issuer_code="ISS01",
            amount=amount,
            due_date=DUE_DATE,
            occurred_at=DUE_AT,
        ),
    )
    assert result.accepted and not result.duplicate


def _fail_first_attempt(
    conn: Conn, cycle_id: str, mandate_id: str = "M1", amount: float = 500.0
) -> None:
    result = ingest_debit_failed(
        conn,
        DebitOutcomeEvent(
            external_id=f"ext:{cycle_id}:fail1",
            mandate_id=mandate_id,
            cycle_id=cycle_id,
            occurred_at=DUE_AT,
            amount=amount,
            raw_reason="INSUFFICIENT FUNDS",
        ),
    )
    assert result.accepted and not result.duplicate


# --- Out-of-order delivery -------------------------------------------------


def test_debit_failed_before_cycle_due_is_a_clean_retryable_error(db: Conn) -> None:
    with pytest.raises(UnknownCycleError):
        ingest_debit_failed(
            db,
            DebitOutcomeEvent(
                external_id="ext:CYC-OOO:fail1",
                mandate_id="M-OOO",
                cycle_id="CYC-OOO",
                occurred_at=DUE_AT,
                amount=500.0,
                raw_reason="INSUFFICIENT FUNDS",
            ),
        )
    # The event insert rolled back with it — no partial row left behind to
    # falsely satisfy the idempotency check on a legitimate retry.
    row = db.execute(
        "SELECT 1 FROM events WHERE external_id = %s", ("ext:CYC-OOO:fail1",)
    ).fetchone()
    assert row is None

    # cycle.due arrives late (the realistic case), then the exact same
    # retried event succeeds cleanly.
    _seed_mandate_and_cycle(db, "CYC-OOO", mandate_id="M-OOO")
    retried = ingest_debit_failed(
        db,
        DebitOutcomeEvent(
            external_id="ext:CYC-OOO:fail1",
            mandate_id="M-OOO",
            cycle_id="CYC-OOO",
            occurred_at=DUE_AT,
            amount=500.0,
            raw_reason="INSUFFICIENT FUNDS",
        ),
    )
    assert retried.accepted and not retried.duplicate


def test_debit_succeeded_before_cycle_due_is_a_clean_retryable_error(db: Conn) -> None:
    with pytest.raises(UnknownCycleError):
        ingest_debit_succeeded(
            db,
            DebitOutcomeEvent(
                external_id="ext:CYC-OOO2:succeed1",
                mandate_id="M-OOO2",
                cycle_id="CYC-OOO2",
                occurred_at=DUE_AT,
                amount=500.0,
            ),
        )
    row = db.execute(
        "SELECT 1 FROM events WHERE external_id = %s", ("ext:CYC-OOO2:succeed1",)
    ).fetchone()
    assert row is None


# --- Stale event after case closure (docs §L.3 red-team exercise) ----------
#
# Found by actually running this exercise, not by inspection: a delayed or
# duplicated seq-1 outcome arriving after the cycle already reached a
# terminal state used to fall straight into reserve_attempt_intent's
# UNIQUE(cycle_id, sequence_no) constraint and surface as a raw, unhandled
# psycopg.errors.UniqueViolation -- a 500, not a clean response. Fixed in
# ingest_debit_succeeded/ingest_debit_failed: a terminal-state cycle now
# quarantines the event (recorded, audited, never re-applied) instead of
# crashing.


def test_stale_debit_succeeded_after_cycle_already_recovered_is_quarantined(
    db: Conn,
) -> None:
    cycle_id = "CYC-STALE1"
    _seed_mandate_and_cycle(db, cycle_id, mandate_id="M-STALE1")
    first = ingest_debit_succeeded(
        db,
        DebitOutcomeEvent(
            external_id=f"ext:{cycle_id}:succeed1",
            mandate_id="M-STALE1",
            cycle_id=cycle_id,
            occurred_at=DUE_AT,
            amount=500.0,
        ),
    )
    assert first.accepted and not first.duplicate
    before = db.execute(
        "SELECT state, attempts_used FROM cycles WHERE id = %s", (cycle_id,)
    ).fetchone()
    assert before["state"] == "RECOVERED" and before["attempts_used"] == 1

    # A delayed duplicate of the same real-world debit outcome, redelivered
    # under a different external_id (a realistic at-least-once rail
    # behaviour, not just a literal retry of the identical payload).
    late = ingest_debit_succeeded(
        db,
        DebitOutcomeEvent(
            external_id=f"ext:{cycle_id}:succeed1:late-redelivery",
            mandate_id="M-STALE1",
            cycle_id=cycle_id,
            occurred_at=DUE_AT + timedelta(hours=1),
            amount=500.0,
        ),
    )
    assert late.accepted and not late.duplicate  # a new event row, not a no-op

    after = db.execute(
        "SELECT state, attempts_used FROM cycles WHERE id = %s", (cycle_id,)
    ).fetchone()
    assert after["state"] == "RECOVERED" and after["attempts_used"] == 1  # untouched

    n_attempts = db.execute(
        "SELECT count(*) AS n FROM attempt_intents WHERE cycle_id = %s", (cycle_id,)
    ).fetchone()["n"]
    assert n_attempts == 1  # no second reservation attempted

    audit = db.execute(
        """SELECT detail FROM audit_ledger
           WHERE cycle_id = %s AND action = 'stale_event_quarantined'""",
        (cycle_id,),
    ).fetchone()
    assert audit is not None
    assert audit["detail"]["event_type"] == "debit.succeeded"
    assert audit["detail"]["cycle_state"] == "RECOVERED"


def test_stale_debit_failed_after_cycle_already_abandoned_is_quarantined(
    db: Conn,
) -> None:
    cycle_id = "CYC-STALE2"
    _seed_mandate_and_cycle(db, cycle_id, mandate_id="M-STALE2")
    db.execute(
        "UPDATE cycles SET state = 'ABANDONED', closed_at = %s WHERE id = %s",
        (DUE_AT, cycle_id),
    )
    db.commit()

    late = ingest_debit_failed(
        db,
        DebitOutcomeEvent(
            external_id=f"ext:{cycle_id}:late-fail",
            mandate_id="M-STALE2",
            cycle_id=cycle_id,
            occurred_at=DUE_AT + timedelta(hours=1),
            amount=500.0,
            raw_reason="INSUFFICIENT FUNDS",
        ),
    )
    assert late.accepted and not late.duplicate  # recorded, not rejected outright

    after = db.execute(
        "SELECT state, attempts_used FROM cycles WHERE id = %s", (cycle_id,)
    ).fetchone()
    assert after["state"] == "ABANDONED" and after["attempts_used"] == 0  # untouched

    n_attempts = db.execute(
        "SELECT count(*) AS n FROM attempt_intents WHERE cycle_id = %s", (cycle_id,)
    ).fetchone()["n"]
    assert n_attempts == 0

    audit = db.execute(
        """SELECT detail FROM audit_ledger
           WHERE cycle_id = %s AND action = 'stale_event_quarantined'""",
        (cycle_id,),
    ).fetchone()
    assert audit is not None
    assert audit["detail"]["event_type"] == "debit.failed"
    assert audit["detail"]["cycle_state"] == "ABANDONED"


# --- Mid-plan mandate lifecycle events -------------------------------------


def test_mandate_revoked_mid_plan_abandons_cycle_and_cancels_pending_steps(
    db: Conn,
) -> None:
    cycle_id = "CYC-REV"
    _seed_mandate_and_cycle(db, cycle_id, mandate_id="M-REV")
    _fail_first_attempt(db, cycle_id, mandate_id="M-REV")

    cycle = repo.get_cycle(db, cycle_id)
    assert cycle is not None and cycle["state"] == "SCHEDULED"
    pending_before = db.execute(
        "SELECT COUNT(*) AS n FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE p.cycle_id = %s AND ps.status = 'pending'",
        (cycle_id,),
    ).fetchone()
    assert pending_before is not None and pending_before["n"] > 0

    result = ingest_mandate_revoked(
        db,
        MandateLifecycleEvent(
            external_id="ext:M-REV:revoked",
            mandate_id="M-REV",
            occurred_at=DUE_AT + timedelta(hours=1),
        ),
    )
    assert result.accepted and not result.duplicate

    cycle_after = repo.get_cycle(db, cycle_id)
    assert cycle_after is not None
    assert cycle_after["state"] == "ABANDONED"
    assert cycle_after["closed_at"] is not None

    pending_after = db.execute(
        "SELECT COUNT(*) AS n FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE p.cycle_id = %s AND ps.status = 'pending'",
        (cycle_id,),
    ).fetchone()
    assert pending_after is not None and pending_after["n"] == 0

    mandate = repo.get_mandate(db, "M-REV")
    assert mandate is not None and mandate["status"] == "revoked"

    audit = db.execute(
        "SELECT * FROM audit_ledger WHERE cycle_id = %s AND action = 'mandate_revoked'",
        (cycle_id,),
    ).fetchone()
    assert audit is not None
    assert audit["detail"]["previous_state"] == "SCHEDULED"

    # The worker ticking forward afterwards is a no-op, not a crash.
    process_due_plan_steps(db, now=DUE_AT + timedelta(days=10))
    still = repo.get_cycle(db, cycle_id)
    assert still is not None and still["state"] == "ABANDONED"


def test_mandate_revoked_is_idempotent(db: Conn) -> None:
    _seed_mandate_and_cycle(db, "CYC-A", mandate_id="M-MULTI")
    _fail_first_attempt(db, "CYC-A", mandate_id="M-MULTI")
    event = MandateLifecycleEvent(
        external_id="ext:M-MULTI:revoked", mandate_id="M-MULTI", occurred_at=DUE_AT
    )
    first = ingest_mandate_revoked(db, event)
    second = ingest_mandate_revoked(db, event)
    assert first.accepted and not first.duplicate
    assert second.duplicate and not second.accepted


def test_notification_opted_out_mid_plan_abandons_cycle(db: Conn) -> None:
    cycle_id = "CYC-OPT"
    _seed_mandate_and_cycle(db, cycle_id, mandate_id="M-OPT")
    _fail_first_attempt(db, cycle_id, mandate_id="M-OPT")

    result = ingest_notification_opted_out(
        db,
        MandateLifecycleEvent(
            external_id="ext:M-OPT:opt",
            mandate_id="M-OPT",
            occurred_at=DUE_AT + timedelta(hours=1),
        ),
    )
    assert result.accepted

    cycle_after = repo.get_cycle(db, cycle_id)
    assert cycle_after is not None and cycle_after["state"] == "ABANDONED"

    mandate = repo.get_mandate(db, "M-OPT")
    assert mandate is not None and mandate["opted_out"] is True

    audit = db.execute(
        "SELECT * FROM audit_ledger WHERE cycle_id = %s AND action = 'opted_out'",
        (cycle_id,),
    ).fetchone()
    assert audit is not None


def test_mandate_revoked_on_already_terminal_cycle_is_a_flag_only_no_op(db: Conn) -> None:
    """A mandate can be revoked after its one cycle already resolved. The
    mandate-level flag still flips; there's simply nothing left to abandon
    — `_abandon_in_flight_cycles` finds no non-terminal cycles."""
    cycle_id = "CYC-DONE"
    _seed_mandate_and_cycle(db, cycle_id, mandate_id="M-DONE")
    result = ingest_debit_succeeded(
        db,
        DebitOutcomeEvent(
            external_id=f"ext:{cycle_id}:succeed1",
            mandate_id="M-DONE",
            cycle_id=cycle_id,
            occurred_at=DUE_AT,
            amount=500.0,
        ),
    )
    assert result.accepted
    cycle = repo.get_cycle(db, cycle_id)
    assert cycle is not None and cycle["state"] == "RECOVERED"

    ingest_mandate_revoked(
        db,
        MandateLifecycleEvent(
            external_id="ext:M-DONE:revoked", mandate_id="M-DONE", occurred_at=DUE_AT
        ),
    )
    mandate = repo.get_mandate(db, "M-DONE")
    assert mandate is not None and mandate["status"] == "revoked"
    cycle_after = repo.get_cycle(db, cycle_id)
    assert cycle_after is not None and cycle_after["state"] == "RECOVERED"  # untouched


def test_mid_flight_revocation_does_not_resurrect_an_abandoned_cycle(
    db: Conn, simulator_base_url: str
) -> None:
    """Regression test for the worker.py `_handle_delivered` race
    (Phase 7): an attempt already dispatched to the outbox (cycle
    EXECUTING) can't be recalled from the rail. If a mandate.revoked event
    resolves the cycle to ABANDONED before the rail answers, the eventual
    outcome must not silently flip the cycle back to RECOVERED."""
    cycle_id = "CYC-RACE"
    _seed_mandate_and_cycle(db, cycle_id, mandate_id="M-RACE")
    _fail_first_attempt(db, cycle_id, mandate_id="M-RACE")

    first_notify = db.execute(
        "SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE p.cycle_id = %s AND ps.step_type = 'notify' ORDER BY ps.scheduled_for LIMIT 1",
        (cycle_id,),
    ).fetchone()
    first_attempt = db.execute(
        "SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE p.cycle_id = %s AND ps.step_type = 'attempt' ORDER BY ps.scheduled_for LIMIT 1",
        (cycle_id,),
    ).fetchone()
    assert first_notify is not None and first_attempt is not None

    process_due_plan_steps(db, now=first_notify["scheduled_for"])
    process_due_plan_steps(db, now=first_attempt["scheduled_for"])  # dispatches -> EXECUTING

    cycle_mid = repo.get_cycle(db, cycle_id)
    assert cycle_mid is not None and cycle_mid["state"] == "EXECUTING"

    # mandate.revoked arrives while this attempt is already in flight at the rail.
    ingest_mandate_revoked(
        db,
        MandateLifecycleEvent(
            external_id="ext:M-RACE:revoked",
            mandate_id="M-RACE",
            occurred_at=first_attempt["scheduled_for"],
        ),
    )
    cycle_after_revoke = repo.get_cycle(db, cycle_id)
    assert cycle_after_revoke is not None and cycle_after_revoke["state"] == "ABANDONED"

    # ... and only now does the rail answer.
    simulator = SimulatorClient(base_url=simulator_base_url)
    drain_outbox(
        db, now=first_attempt["scheduled_for"] + timedelta(seconds=1), simulator=simulator
    )

    cycle_final = repo.get_cycle(db, cycle_id)
    assert cycle_final is not None
    assert cycle_final["state"] == "ABANDONED"  # not resurrected to RECOVERED
    audit = db.execute(
        "SELECT * FROM audit_ledger WHERE cycle_id = %s "
        "AND action = 'attempt_outcome_after_cycle_resolved'",
        (cycle_id,),
    ).fetchone()
    assert audit is not None
    assert audit["detail"]["cycle_state"] == "ABANDONED"
