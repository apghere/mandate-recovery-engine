"""End-to-end integration: real Postgres (docker compose `db`), a real
simulator server over HTTP, and the actual ingestion + worker + outbox code
path — no in-process shortcuts (docs H.2).

Requires `make up` to have been run. Skips gracefully if Postgres isn't
reachable (see conftest.py's `db` fixture).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
from app import repo
from app.adapters.simulator_client import SimulatorClient
from app.db import Conn
from app.ingest import (
    CycleDueEvent,
    DebitOutcomeEvent,
    ingest_cycle_due,
    ingest_debit_failed,
    ingest_debit_succeeded,
)
from app.policies.fixed import ATTEMPT_OFFSET_DAYS
from app.workflows.worker import drain_outbox, process_due_plan_steps, sweep_exhausted_plans

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


def test_cycle_due_ingestion_is_idempotent(db: Conn) -> None:
    event = CycleDueEvent(
        external_id="ext:dup",
        mandate_id="M1",
        cycle_id="CYC-DUP",
        merchant_id="MERCH1",
        payer_id="PAYER1",
        rail="upi_autopay",
        issuer_code="ISS01",
        amount=500.0,
        due_date=DUE_DATE,
        occurred_at=DUE_AT,
    )
    first = ingest_cycle_due(db, event)
    second = ingest_cycle_due(db, event)
    assert first.accepted and not first.duplicate
    assert second.duplicate and not second.accepted
    assert repo.count_attempts(db, "CYC-DUP") == 0  # no plan seeded by cycle.due alone


def test_first_attempt_succeeded_resolves_without_engaging_mre(db: Conn) -> None:
    _seed_mandate_and_cycle(db, "CYC-S1")
    result = ingest_debit_succeeded(
        db,
        DebitOutcomeEvent(
            external_id="ext:CYC-S1:succeed1",
            mandate_id="M1",
            cycle_id="CYC-S1",
            occurred_at=DUE_AT,
            amount=500.0,
        ),
    )
    assert result.accepted
    cycle = repo.get_cycle(db, "CYC-S1")
    assert cycle is not None
    assert cycle["state"] == "RECOVERED"
    assert float(cycle["recovered_amount"]) == 500.0
    assert repo.count_attempts(db, "CYC-S1") == 1
    # No plan was ever created — MRE never engaged.
    plan_row = db.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE cycle_id = %s", ("CYC-S1",)
    ).fetchone()
    assert plan_row is not None
    assert plan_row["n"] == 0


def test_first_attempt_failure_is_normalized_and_persisted(db: Conn) -> None:
    """docs K.2 end-to-end: the raw decline string on the external seq-1
    failure gets normalized (dictionary match here, since "INSUFFICIENT
    FUNDS" is a literal taxonomy template — no LLM/network involved) and
    the result actually lands on the attempt_intents row, not just logged
    somewhere."""
    _seed_mandate_and_cycle(db, "CYC-NORM")
    _fail_first_attempt(db, "CYC-NORM")
    row = db.execute(
        "SELECT canonical_cause, cause_confidence, cause_source, raw_reason "
        "FROM attempt_intents WHERE cycle_id = %s AND sequence_no = 1",
        ("CYC-NORM",),
    ).fetchone()
    assert row is not None
    assert row["canonical_cause"] == "INSUFFICIENT_FUNDS"
    assert row["cause_confidence"] == 1.0
    assert row["cause_source"] == "dictionary"
    assert row["raw_reason"] == "INSUFFICIENT FUNDS"

    audit_row = db.execute(
        "SELECT detail FROM audit_ledger WHERE cycle_id = %s AND action = 'cause_normalized'",
        ("CYC-NORM",),
    ).fetchone()
    assert audit_row is not None
    assert audit_row["detail"]["cause"] == "INSUFFICIENT_FUNDS"


def test_first_attempt_failed_seeds_exactly_three_remaining_attempt_steps(db: Conn) -> None:
    _seed_mandate_and_cycle(db, "CYC-F1")
    _fail_first_attempt(db, "CYC-F1")
    cycle = repo.get_cycle(db, "CYC-F1")
    assert cycle is not None
    assert cycle["state"] == "SCHEDULED"
    assert cycle["attempts_used"] == 1

    attempt_steps = db.execute(
        """
        SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id
        WHERE p.cycle_id = %s AND ps.step_type = 'attempt'
        """,
        ("CYC-F1",),
    ).fetchall()
    notify_steps = db.execute(
        """
        SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id
        WHERE p.cycle_id = %s AND ps.step_type = 'notify'
        """,
        ("CYC-F1",),
    ).fetchall()
    assert len(attempt_steps) == len(ATTEMPT_OFFSET_DAYS) == 3
    assert len(notify_steps) == 3
    for notify, attempt in zip(
        sorted(notify_steps, key=lambda r: r["scheduled_for"]),
        sorted(attempt_steps, key=lambda r: r["scheduled_for"]),
        strict=True,
    ):
        assert notify["scheduled_for"] < attempt["scheduled_for"]
        assert attempt["scheduled_for"] - notify["scheduled_for"] >= timedelta(hours=24)


def test_worker_denies_attempt_without_a_satisfying_notice(
    db: Conn, simulator_base_url: str
) -> None:
    """The killer-demo scenario (docs W3 / R 2:35-3:05): if the notice
    never went out, the policy engine denies the debit at execution time —
    independently of the plan — and the attempt is not consumed."""
    _seed_mandate_and_cycle(db, "CYC-NN")
    _fail_first_attempt(db, "CYC-NN")

    first_attempt = db.execute(
        """
        SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id
        WHERE p.cycle_id = %s AND ps.step_type = 'attempt'
        ORDER BY ps.scheduled_for LIMIT 1
        """,
        ("CYC-NN",),
    ).fetchone()
    first_notify = db.execute(
        """
        SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id
        WHERE p.cycle_id = %s AND ps.step_type = 'notify'
        ORDER BY ps.scheduled_for LIMIT 1
        """,
        ("CYC-NN",),
    ).fetchone()
    assert first_attempt is not None and first_notify is not None

    # Simulate a notice-send failure directly: the notify step never runs
    # (and so never creates a `notifications` row), rather than skipping it
    # via clock manipulation — fetch_due_plan_steps would pick it up anyway
    # since it's scheduled earlier than the attempt.
    repo.mark_plan_step(
        db, first_notify["id"], status="cancelled", cancelled_reason="test_simulated_notice_failure"
    )
    db.commit()
    process_due_plan_steps(db, now=first_attempt["scheduled_for"])

    step_after = db.execute(
        "SELECT * FROM plan_steps WHERE id = %s", (first_attempt["id"],)
    ).fetchone()
    assert step_after is not None
    assert step_after["status"] == "cancelled"
    assert step_after["cancelled_reason"] == "RBI_NOTICE_NOT_SATISFIED"
    assert repo.count_attempts(db, "CYC-NN") == 1  # only seq 1 — not consumed
    cycle = repo.get_cycle(db, "CYC-NN")
    assert cycle is not None
    assert cycle["state"] == "SCHEDULED"  # unchanged, plan continues


def test_afa_gated_cycle_reaches_abandoned_not_stuck_forever(
    db: Conn, simulator_base_url: str
) -> None:
    """Regression test: found via scripts/replay_fixed.py on 500 real
    generated mandates. An amount above the AFA threshold with no consent
    flow modelled (Phase 3 simplification, worker.py's _build_snapshot
    always sets afa_satisfied=False) means every attempt is denied at the
    gate — none are ever consumed, so a real BUDGET_EXHAUSTED via a 4th
    attempt never fires, and without sweep_exhausted_plans the cycle stays
    in SCHEDULED forever once its plan runs out of steps."""
    cycle_id = "CYC-AFA"
    amount = 20_000.0  # above AFA_THRESHOLD_DEFAULT (15,000)
    _seed_mandate_and_cycle(db, cycle_id, amount=amount)
    _fail_first_attempt(db, cycle_id, amount=amount)

    attempt_steps = db.execute(
        """
        SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id
        WHERE p.cycle_id = %s AND ps.step_type = 'attempt'
        ORDER BY ps.scheduled_for
        """,
        (cycle_id,),
    ).fetchall()
    notify_steps = db.execute(
        """
        SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id
        WHERE p.cycle_id = %s AND ps.step_type = 'notify'
        ORDER BY ps.scheduled_for
        """,
        (cycle_id,),
    ).fetchall()
    for step in notify_steps + attempt_steps:
        process_due_plan_steps(db, now=step["scheduled_for"])

    # All three remaining attempts denied at the gate — none consumed.
    assert repo.count_attempts(db, cycle_id) == 1  # only seq 1 (the external failure)
    cycle_before_sweep = repo.get_cycle(db, cycle_id)
    assert cycle_before_sweep is not None
    assert cycle_before_sweep["state"] == "SCHEDULED"  # would stay here forever pre-fix

    swept = sweep_exhausted_plans(db, now=attempt_steps[-1]["scheduled_for"])
    assert swept == 1

    cycle_after = repo.get_cycle(db, cycle_id)
    assert cycle_after is not None
    assert cycle_after["state"] == "ABANDONED"
    assert cycle_after["closed_at"] is not None
    assert float(cycle_after["recovered_amount"]) == 0.0


def test_worker_dispatched_attempts_carry_real_payer_context_not_none(
    db: Conn, simulator_base_url: str
) -> None:
    """Regression test (Phase 8 prep): worker.py used to hardcode
    payer_id=None and omit issuer_code/timing context entirely on every
    outbox payload for attempts 2-4 — exactly the attempts a policy
    actually schedules — so the live simulator silently fell back to the
    same flat, payer-independent probability for everyone regardless of
    who they were or when the attempt landed."""
    cycle_id = "CYC-CTX"
    payer_id = "PAYER-CTX"
    repo.upsert_payer(
        db,
        payer_id=payer_id,
        segment="salaried",
        credit_day=5,
        mean_balance=8000.0,
        balance_volatility=0.4,
        issuer_code="ISS01",
        chronic_fail_propensity=0.1,
        annoyance_sensitivity=0.5,
        mandate_amount=500.0,
        split="dev",
    )
    db.commit()

    result = ingest_cycle_due(
        db,
        CycleDueEvent(
            external_id=f"ext:{cycle_id}:due",
            mandate_id="M-CTX",
            cycle_id=cycle_id,
            merchant_id="MERCH1",
            payer_id=payer_id,
            rail="upi_autopay",
            issuer_code="ISS01",
            amount=500.0,
            due_date=DUE_DATE,
            occurred_at=DUE_AT,
        ),
    )
    assert result.accepted
    _fail_first_attempt(db, cycle_id, mandate_id="M-CTX")

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
    process_due_plan_steps(db, now=first_attempt["scheduled_for"])

    outbox_row = db.execute(
        "SELECT payload FROM outbox WHERE payload->>'cycle_id' = %s", (cycle_id,)
    ).fetchone()
    assert outbox_row is not None
    payload = outbox_row["payload"]
    assert payload["payer_id"] == payer_id
    assert payload["issuer_code"] == "ISS01"
    assert payload["chronic_fail_propensity"] == 0.1
    assert payload["mean_balance"] == 8000.0
    assert payload["balance_volatility"] == 0.4
    assert payload["credit_day"] == 5


def test_worker_dispatched_attempt_without_a_payers_row_degrades_to_flat_fallback(
    db: Conn, simulator_base_url: str
) -> None:
    """No payers row for this mandate's payer_id (the common case in most
    other tests in this file) — the outbox payload should carry the real
    issuer_code (known from the mandate itself) but None for the
    payer-specific timing fields, matching decide_outcome's documented
    flat-fallback contract rather than crashing."""
    _seed_mandate_and_cycle(db, "CYC-NOPAYER")
    _fail_first_attempt(db, "CYC-NOPAYER")

    first_notify = db.execute(
        "SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE p.cycle_id = %s AND ps.step_type = 'notify' ORDER BY ps.scheduled_for LIMIT 1",
        ("CYC-NOPAYER",),
    ).fetchone()
    first_attempt = db.execute(
        "SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE p.cycle_id = %s AND ps.step_type = 'attempt' ORDER BY ps.scheduled_for LIMIT 1",
        ("CYC-NOPAYER",),
    ).fetchone()
    assert first_notify is not None and first_attempt is not None
    process_due_plan_steps(db, now=first_notify["scheduled_for"])
    process_due_plan_steps(db, now=first_attempt["scheduled_for"])

    outbox_row = db.execute(
        "SELECT payload FROM outbox WHERE payload->>'cycle_id' = %s", ("CYC-NOPAYER",)
    ).fetchone()
    assert outbox_row is not None
    payload = outbox_row["payload"]
    assert payload["issuer_code"] == "ISS01"  # known from the mandate regardless
    assert payload["mean_balance"] is None
    assert payload["balance_volatility"] is None
    assert payload["credit_day"] is None


def test_outbox_retries_on_rail_5xx_then_delivers(db: Conn, simulator_base_url: str) -> None:
    _seed_mandate_and_cycle(db, "CYC-5XX")
    _fail_first_attempt(db, "CYC-5XX")

    first_notify = db.execute(
        """
        SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id
        WHERE p.cycle_id = %s AND ps.step_type = 'notify'
        ORDER BY ps.scheduled_for LIMIT 1
        """,
        ("CYC-5XX",),
    ).fetchone()
    first_attempt = db.execute(
        """
        SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id
        WHERE p.cycle_id = %s AND ps.step_type = 'attempt'
        ORDER BY ps.scheduled_for LIMIT 1
        """,
        ("CYC-5XX",),
    ).fetchone()
    assert first_notify is not None and first_attempt is not None

    process_due_plan_steps(db, now=first_notify["scheduled_for"])
    process_due_plan_steps(db, now=first_attempt["scheduled_for"])

    outbox_row = db.execute(
        "SELECT * FROM outbox WHERE payload->>'cycle_id' = %s", ("CYC-5XX",)
    ).fetchone()
    assert outbox_row is not None
    assert outbox_row["delivered_at"] is None

    simulator = SimulatorClient(base_url=simulator_base_url)
    httpx.post(
        f"{simulator_base_url}/admin/chaos", json={"error_5xx_rate": 1.0, "timeout_rate": 0.0}
    )
    try:
        drain = drain_outbox(db, now=first_attempt["scheduled_for"], simulator=simulator)
        assert drain.retried == 1 and drain.delivered == 0

        still_pending = db.execute(
            "SELECT delivered_at, attempts FROM outbox WHERE id = %s", (outbox_row["id"],)
        ).fetchone()
        assert still_pending is not None
        assert still_pending["delivered_at"] is None
        assert still_pending["attempts"] == 1
    finally:
        httpx.post(
            f"{simulator_base_url}/admin/chaos", json={"error_5xx_rate": 0.0, "timeout_rate": 0.0}
        )

    drain2 = drain_outbox(
        db, now=first_attempt["scheduled_for"] + timedelta(seconds=60), simulator=simulator
    )
    assert drain2.delivered == 1
    delivered_row = db.execute(
        "SELECT delivered_at FROM outbox WHERE id = %s", (outbox_row["id"],)
    ).fetchone()
    assert delivered_row is not None
    assert delivered_row["delivered_at"] is not None


def test_full_replay_reaches_a_terminal_state_within_invariants(
    db: Conn, simulator_base_url: str
) -> None:
    """Not pinned to a specific stochastic outcome — asserts the invariants
    that must hold regardless of how the simulator's random draws land."""
    cycle_id = "CYC-FULL"
    _seed_mandate_and_cycle(db, cycle_id)
    _fail_first_attempt(db, cycle_id)

    simulator = SimulatorClient(base_url=simulator_base_url)
    clock = DUE_AT
    horizon_end = DUE_AT + timedelta(days=14)
    step = timedelta(hours=1)
    while clock < horizon_end:
        process_due_plan_steps(db, now=clock)
        drain_outbox(db, now=clock, simulator=simulator)
        sweep_exhausted_plans(db, now=clock)
        cycle = repo.get_cycle(db, cycle_id)
        assert cycle is not None
        if cycle["state"] in ("RECOVERED", "ABANDONED"):
            break
        clock += step

    cycle = repo.get_cycle(db, cycle_id)
    assert cycle is not None
    assert cycle["state"] in ("RECOVERED", "ABANDONED"), (
        f"cycle did not reach a terminal state within the horizon: {cycle['state']}"
    )
    assert 1 <= cycle["attempts_used"] <= 4
    assert repo.count_attempts(db, cycle_id) == cycle["attempts_used"]
    if cycle["state"] == "RECOVERED":
        assert float(cycle["recovered_amount"]) == 500.0
    else:
        assert float(cycle["recovered_amount"]) == 0.0
    assert cycle["closed_at"] is not None
