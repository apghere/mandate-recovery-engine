"""MRE wired into real ingestion + the real worker (docs §W2, §O.4's
Phase 5 "register it as policy mre" prompt) — against live Postgres, not
mocks.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from app import repo
from app.db import Conn
from app.domain.planner import PlannerConfig, PlanningInputs
from app.ingest import (
    CycleDueEvent,
    DebitOutcomeEvent,
    PlanChoice,
    ingest_cycle_due,
    ingest_debit_failed,
)
from app.policies.mre import compute_mre_schedule
from app.workflows.worker import process_due_plan_steps

DUE_DATE = date(2026, 9, 1)
DUE_AT = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)


def _seed_cycle(conn: Conn, cycle_id: str, amount: float = 500.0) -> None:
    result = ingest_cycle_due(
        conn,
        CycleDueEvent(
            external_id=f"ext:{cycle_id}:due",
            mandate_id="M-MRE",
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
    assert result.accepted


def _mre_compute_plan(p_success: tuple[float, ...]) -> object:
    config = PlannerConfig(n_slots=len(p_success), max_attempts=4)

    def compute_plan(due_date: date) -> PlanChoice:
        inputs = PlanningInputs(
            amount=500.0, p_success=p_success, e_manual=200.0, e_manual_late=50.0
        )
        plan = compute_mre_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version="MRE-dp-v1",
            steps=plan.steps,
            immediate_stop=plan.immediate_stop,
            expected_value=plan.expected_value,
            solver_ms=plan.solver_ms,
        )

    return compute_plan


def test_mre_zero_probability_escalates_immediately_zero_attempts_consumed(db: Conn) -> None:
    """docs §W2: cause -> planner values every continuation at ~zero ->
    stopping rule fires -> zero attempts consumed -> AWAITING_MANUAL."""
    cycle_id = "CYC-MRE-STOP"
    _seed_cycle(db, cycle_id)
    result = ingest_debit_failed(
        db,
        DebitOutcomeEvent(
            external_id=f"ext:{cycle_id}:fail1",
            mandate_id="M-MRE",
            cycle_id=cycle_id,
            occurred_at=DUE_AT,
            amount=500.0,
            raw_reason="MANDATE REVOKED",
        ),
        compute_plan=_mre_compute_plan(tuple([0.0] * 28)),
    )
    assert result.accepted

    cycle = repo.get_cycle(db, cycle_id)
    assert cycle is not None
    assert cycle["state"] == "AWAITING_MANUAL"
    assert cycle["attempts_used"] == 1  # only the external seq-1 attempt

    plan_row = db.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE cycle_id = %s", (cycle_id,)
    ).fetchone()
    assert plan_row is not None
    assert plan_row["n"] == 0  # no plan/plan_steps created for an immediate stop

    audit_row = db.execute(
        "SELECT action, detail FROM audit_ledger WHERE cycle_id = %s ORDER BY id DESC LIMIT 1",
        (cycle_id,),
    ).fetchone()
    assert audit_row is not None
    assert audit_row["action"] == "stopped_and_escalated"
    assert audit_row["detail"]["policy"] == "MRE-dp-v1"


def test_mre_favorable_probability_schedules_a_real_plan(db: Conn) -> None:
    cycle_id = "CYC-MRE-GO"
    _seed_cycle(db, cycle_id)
    result = ingest_debit_failed(
        db,
        DebitOutcomeEvent(
            external_id=f"ext:{cycle_id}:fail1",
            mandate_id="M-MRE",
            cycle_id=cycle_id,
            occurred_at=DUE_AT,
            amount=500.0,
            raw_reason="INSUFFICIENT FUNDS",
        ),
        compute_plan=_mre_compute_plan(tuple([0.8] * 28)),
    )
    assert result.accepted

    cycle = repo.get_cycle(db, cycle_id)
    assert cycle is not None
    assert cycle["state"] == "SCHEDULED"

    plan_row = db.execute(
        "SELECT id, model_version FROM plans WHERE cycle_id = %s", (cycle_id,)
    ).fetchone()
    assert plan_row is not None
    assert plan_row["model_version"] == "MRE-dp-v1"

    steps = db.execute(
        "SELECT * FROM plan_steps WHERE plan_id = %s ORDER BY scheduled_for", (plan_row["id"],)
    ).fetchall()
    assert len(steps) > 0
    assert any(s["step_type"] == "attempt" for s in steps)

    # The existing (policy-agnostic) worker can process this plan exactly
    # like it processes P0's -- that's the whole point of the shared
    # (step_type, scheduled_for) shape.
    first_step = steps[0]
    process_due_plan_steps(db, now=first_step["scheduled_for"])
    step_after = db.execute(
        "SELECT status FROM plan_steps WHERE id = %s", (first_step["id"],)
    ).fetchone()
    assert step_after is not None
    assert step_after["status"] in ("done", "dispatched")
