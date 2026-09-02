"""Seed a small, curated, repeatable set of cases for a live demo of the
dashboard (docs Day 6: "Seed/reset script so the demo is repeatable").

Not `make replay-fixed`/`make replay-compare`'s job (500/300 payers each,
built for aggregate statistics, not for "click into a specific case and
see a specific, understandable story"). This seeds a handful of
hand-picked scenarios that each demonstrate one of docs §I.5's four core
workflows end to end, through the REAL `/events`-equivalent ingestion path
(app.ingest + app.workflows.worker + app.policies.live) — not a shortcut —
plus a modest background batch of real `dev`-split payers through the
same live path so the case list and /metrics don't look suspiciously
empty.

W2 (stop-and-escalate) honesty note: the live scorer does not currently
discriminate on decline cause at all, and empirically, no realistic
(amount, balance, volatility) combination reliably drives the trained
model's probability low enough to make continuing worse than E_MANUAL=150
across a 3-attempt budget (both are documented gaps -- see CLAUDE.md's
Phase 9 notes and README.md's Limitations 3 and 4). Rather than fake a
"live" trigger that doesn't actually occur through the real scorer today,
the W2 case here supplies a directly-constructed low-probability plan to
the same real DP solver (`app.policies.mre.compute_mre_schedule`) --
exactly what tests/integration/test_mre_ingestion.py already does to unit-
test the stopping rule -- and says so, rather than silently presenting it
as something the live scorer produced on its own.

Usage: `make demo-seed` (wipes and reseeds -- do not point this at
anything you care about). Run it immediately before a live demo/recording,
not hours in advance: `make test`/`make check` truncate the same tables
via the integration test fixtures and will leave whatever the last test
happened to insert instead of these curated cases.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import uvicorn
from app import repo
from app.adapters.simulator_client import SimulatorClient
from app.db import Conn, get_connection
from app.domain.planner import PlannerConfig, PlanningInputs
from app.domain.types import Cause
from app.ingest import (
    CycleDueEvent,
    DebitOutcomeEvent,
    PlanChoice,
    ingest_cycle_due,
    ingest_debit_failed,
    ingest_debit_succeeded,
)
from app.policies.live import select_compute_plan
from app.policies.mre import POLICY_VERSION as MRE_VERSION
from app.policies.mre import compute_mre_schedule
from app.repo import upsert_payer
from app.workflows.worker import drain_outbox, process_due_plan_steps, sweep_exhausted_plans

from data.generator import generate_population
from simulator.app import create_app

DUE_DATE = date(2026, 9, 3)
DUE_AT = datetime(2026, 9, 3, 2, 0, tzinfo=UTC)
N_BACKGROUND = 40

_RESET_TABLES = (
    "audit_ledger",
    "decisions",
    "notifications",
    "outbox",
    "attempt_intents",
    "plan_steps",
    "plans",
    "cycles",
    "mandates",
    "events",
    "kill_switches",
)


def _start_simulator() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(db_path=":memory:")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "in-process simulator failed to start"
    return f"http://127.0.0.1:{port}"


def _seed_cycle_and_attempt1(
    conn: Conn,
    simulator: SimulatorClient,
    cycle_id: str,
    mandate_id: str,
    payer_id: str,
    amount: float,
    issuer_code: str,
) -> tuple[DebitOutcomeEvent, bool]:
    ingest_cycle_due(
        conn,
        CycleDueEvent(
            external_id=f"ext:{cycle_id}:due", mandate_id=mandate_id, cycle_id=cycle_id,
            merchant_id="MERCH-DEMO", payer_id=payer_id, rail="upi_autopay",
            issuer_code=issuer_code, amount=amount, due_date=DUE_DATE, occurred_at=DUE_AT,
        ),
    )
    payer_row = repo.get_payer(conn, payer_id)
    kwargs: dict[str, Any] = {}
    if payer_row is not None:
        kwargs = {
            "mean_balance": float(payer_row["mean_balance"]),
            "balance_volatility": payer_row["balance_volatility"],
            "credit_day": payer_row["credit_day"],
            "chronic_fail_propensity": payer_row["chronic_fail_propensity"],
        }
    result = simulator.execute(
        cycle_id=cycle_id, sequence_no=1, idempotency_key=f"{cycle_id}:seq1:demo",
        mandate_id=mandate_id, payer_id=payer_id, amount=amount, scheduled_for=DUE_AT,
        issuer_code=issuer_code, **kwargs,
    )
    outcome_event = DebitOutcomeEvent(
        external_id=f"ext:{cycle_id}:attempt1", mandate_id=mandate_id, cycle_id=cycle_id,
        occurred_at=DUE_AT, amount=amount, raw_reason=result.raw_reason,
    )
    return outcome_event, result.outcome == "success"


def _hopeless_compute_plan(amount: float) -> Callable[[date, Cause], PlanChoice]:
    """W2's curated illustration -- see module docstring's honesty note.
    Supplies a directly-constructed p_success=0.0 curve to the real DP
    solver, the same technique tests/integration/test_mre_ingestion.py
    uses to unit-test the stopping rule in isolation.

    Not p=0.02 or any other "low but plausible" value: empirically (see
    CLAUDE.md's Phase 9 notes), with the current cost defaults
    (E_MANUAL=150, PlannerConfig's hazard constants) the stopping rule
    only fires at essentially zero probability -- even p=0.001 makes
    continuing worth more than stopping for any realistic mandate amount,
    since E_MANUAL is a fixed cost and the expected value of continuing
    scales with amount. That is itself a real, honestly-flagged limitation
    (README.md's Limitation 3), not something this demo script should
    paper over by hand-picking a threshold that happens to trigger."""

    def compute_plan(due_date: date, _cause: Cause) -> PlanChoice:
        config = PlannerConfig(n_slots=28, max_attempts=4)
        inputs = PlanningInputs(
            amount=amount, p_success=tuple([0.0] * 28), e_manual=150.0, e_manual_late=30.0
        )
        plan = compute_mre_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version=MRE_VERSION, steps=plan.steps, immediate_stop=plan.immediate_stop,
            expected_value=plan.expected_value, solver_ms=plan.solver_ms,
        )

    return compute_plan


def main() -> None:
    print("starting in-process simulator...")
    base_url = _start_simulator()
    simulator = SimulatorClient(base_url=base_url)

    with get_connection() as conn:
        print("wiping demo-relevant tables (payers left untouched)...")
        conn.execute(f"TRUNCATE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()

        print("seeding curated scenarios...")

        # W1 -- automatic recovery: a payer with real, decent funds context
        # who fails once (external attempt 1) then recovers via the live,
        # payer-aware MRE plan on a later attempt.
        upsert_payer(
            conn, payer_id="PAYER-DEMO-RECOVERY", segment="salaried", credit_day=5,
            mean_balance=12000.0, balance_volatility=0.3, issuer_code="ISS01",
            chronic_fail_propensity=0.05, annoyance_sensitivity=0.3, mandate_amount=1500.0,
            split="dev",
        )
        outcome, succeeded = _seed_cycle_and_attempt1(
            conn, simulator, "CYC-0-RECOVERY", "MANDATE-0-RECOVERY",
            "PAYER-DEMO-RECOVERY", 1500.0, "ISS01",
        )
        if succeeded:
            ingest_debit_succeeded(conn, outcome)
        else:
            mandate = repo.get_mandate(conn, "MANDATE-0-RECOVERY")
            assert mandate is not None
            ingest_debit_failed(conn, outcome, compute_plan=select_compute_plan(conn, mandate))

        # W2 -- stop and escalate: curated low-probability plan (see
        # _hopeless_compute_plan's docstring for the honesty note).
        ingest_cycle_due(
            conn,
            CycleDueEvent(
                external_id="ext:CYC-0-HOPELESS:due", mandate_id="MANDATE-0-HOPELESS",
                cycle_id="CYC-0-HOPELESS", merchant_id="MERCH-DEMO",
                payer_id="PAYER-DEMO-HOPELESS", rail="upi_autopay", issuer_code="ISS01",
                amount=8000.0, due_date=DUE_DATE, occurred_at=DUE_AT,
            ),
        )
        hopeless_result = simulator.execute(
            cycle_id="CYC-0-HOPELESS", sequence_no=1,
            idempotency_key="CYC-0-HOPELESS:seq1:demo", mandate_id="MANDATE-0-HOPELESS",
            payer_id="PAYER-DEMO-HOPELESS", amount=8000.0, scheduled_for=DUE_AT,
            issuer_code="ISS01",
        )
        hopeless_event = DebitOutcomeEvent(
            external_id="ext:CYC-0-HOPELESS:attempt1", mandate_id="MANDATE-0-HOPELESS",
            cycle_id="CYC-0-HOPELESS", occurred_at=DUE_AT, amount=8000.0,
            raw_reason=hopeless_result.raw_reason,
        )
        if hopeless_result.outcome == "success":
            ingest_debit_succeeded(conn, hopeless_event)  # unlikely, but handle it
        else:
            ingest_debit_failed(
                conn, hopeless_event, compute_plan=_hopeless_compute_plan(8000.0)
            )

        # W3 -- compliance-blocked execution ("the demo failure"): a real
        # MRE plan, then the first notice is made to fail to send (docs
        # §W3), so the first attempt gets denied RBI_NOTICE_NOT_SATISFIED
        # at execution time -- independently of what the plan assumed.
        upsert_payer(
            conn, payer_id="PAYER-DEMO-BLOCKED", segment="salaried", credit_day=8,
            mean_balance=9000.0, balance_volatility=0.35, issuer_code="ISS01",
            chronic_fail_propensity=0.05, annoyance_sensitivity=0.3, mandate_amount=1200.0,
            split="dev",
        )
        outcome3, succeeded3 = _seed_cycle_and_attempt1(
            conn, simulator, "CYC-0-BLOCKED", "MANDATE-0-BLOCKED",
            "PAYER-DEMO-BLOCKED", 1200.0, "ISS01",
        )
        if not succeeded3:
            mandate3 = repo.get_mandate(conn, "MANDATE-0-BLOCKED")
            assert mandate3 is not None
            ingest_debit_failed(conn, outcome3, compute_plan=select_compute_plan(conn, mandate3))
            first_notify = conn.execute(
                "SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
                "WHERE p.cycle_id = %s AND ps.step_type = 'notify' "
                "ORDER BY ps.scheduled_for LIMIT 1",
                ("CYC-0-BLOCKED",),
            ).fetchone()
            if first_notify is not None:
                repo.mark_plan_step(
                    conn, first_notify["id"], status="cancelled",
                    cancelled_reason="demo_simulated_notice_failure",
                )
                conn.commit()

        # Background batch: real dev-split payers through the same live
        # path, so the dashboard's case list and /metrics look like a
        # real system in use, not four lonely rows.
        print(f"seeding {N_BACKGROUND} background cases from the dev split...")
        payers = [p for p in generate_population() if p.split == "dev"][:N_BACKGROUND]
        for payer in payers:
            cycle_id = f"CYC-BG-{payer.payer_id}"
            mandate_id = f"MANDATE-BG-{payer.payer_id}"
            upsert_payer(
                conn, payer_id=payer.payer_id, segment=payer.segment,
                credit_day=payer.credit_day, mean_balance=payer.mean_balance,
                balance_volatility=payer.balance_volatility, issuer_code=payer.issuer_code,
                chronic_fail_propensity=payer.chronic_fail_propensity,
                annoyance_sensitivity=payer.annoyance_sensitivity,
                mandate_amount=payer.mandate_amount, split=payer.split,
            )
            outcome_bg, succeeded_bg = _seed_cycle_and_attempt1(
                conn, simulator, cycle_id, mandate_id, payer.payer_id, payer.mandate_amount,
                payer.issuer_code,
            )
            if succeeded_bg:
                ingest_debit_succeeded(conn, outcome_bg)
            else:
                mandate_bg = repo.get_mandate(conn, mandate_id)
                assert mandate_bg is not None
                ingest_debit_failed(
                    conn, outcome_bg, compute_plan=select_compute_plan(conn, mandate_bg)
                )

        print("running every scheduled step to completion...")
        ticks = 0
        while True:
            row = conn.execute(
                "SELECT MIN(scheduled_for) AS t FROM plan_steps WHERE status = 'pending'"
            ).fetchone()
            assert row is not None
            now = row["t"]
            if now is None or now > DUE_AT + timedelta(days=21):
                break
            process_due_plan_steps(conn, now=now)
            drain_outbox(conn, now=now, simulator=simulator)
            sweep_exhausted_plans(conn, now=now)
            ticks += 1

    print()
    print(f"done -- {ticks} clock ticks. Curated cases:")
    print("  CYC-0-RECOVERY  (W1 automatic recovery)")
    print("  CYC-0-HOPELESS  (W2 stop-and-escalate -- curated, see module docstring)")
    print("  CYC-0-BLOCKED   (W3 compliance-blocked execution, the demo failure)")
    print(f"  + {len(payers)} background cases from the dev split")
    print()
    print("Start the API + dashboard: .venv/bin/python -m uvicorn app.api.app:app --reload")
    print("Then visit http://localhost:8000/dashboard/index.html")


if __name__ == "__main__":
    main()
