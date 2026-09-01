"""Phase 3 smoke replay: P0 fixed-schedule policy over N synthetic mandates,
end to end through the real ingestion -> worker -> outbox -> simulator path.

This is CLAUDE.md's Phase 3 definition of done ("500-mandate fixed-policy
replay completes"), NOT the sealed Day-5 paired-policy benchmark
(evaluation/runner.py, Phase 8) — which is why this deliberately draws its
mandates from the `dev` split, never `test`. Docs §J.5: the sealed test
split is "touched exactly once, on Day 5"; reusing it here for routine
development smoke-testing would be exactly the mistake §T's red-team
point 3 warns about, however well-intentioned.

Simplification, documented rather than hidden: every mandate shares one
`due_date` for now, so P0's fixed offsets land on identical timestamps
across the whole batch — which is also *why* this replay is fast (a
handful of distinct clock instants, not 500 independent schedules). A
realistic spread of due dates is Phase 8 scope, once the case/event
generator referenced in docs/DATA_MODEL.md exists.

Usage: `make replay-fixed` (spins up its own in-process simulator; wipes
and reseeds the relevant tables in DATABASE_URL — do not point this at
anything you care about).
"""
from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, date, datetime

import uvicorn
from app import repo
from app.adapters.simulator_client import SimulatorClient
from app.db import get_connection
from app.ingest import (
    CycleDueEvent,
    DebitOutcomeEvent,
    ingest_cycle_due,
    ingest_debit_failed,
    ingest_debit_succeeded,
)
from app.workflows.worker import drain_outbox, process_due_plan_steps, sweep_exhausted_plans

from data.generator import generate_population
from simulator.app import create_app

N_MANDATES = 500
DUE_DATE = date(2026, 9, 1)
DUE_AT = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)

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


def main() -> None:
    print("starting in-process simulator...")
    base_url = _start_simulator()
    simulator = SimulatorClient(base_url=base_url)

    payers = [p for p in generate_population() if p.split == "dev"][:N_MANDATES]
    print(f"seeding {len(payers)} mandates from the DEV split (never `test`)...")

    with get_connection() as conn:
        conn.execute(f"TRUNCATE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()

        cycle_ids: list[str] = []
        for payer in payers:
            mandate_id = f"MANDATE-{payer.payer_id}"
            cycle_id = f"CYC-{payer.payer_id}"
            cycle_ids.append(cycle_id)
            ingest_cycle_due(
                conn,
                CycleDueEvent(
                    external_id=f"ext:{cycle_id}:due",
                    mandate_id=mandate_id,
                    cycle_id=cycle_id,
                    merchant_id="MERCH-REPLAY",
                    payer_id=payer.payer_id,
                    rail="upi_autopay",
                    issuer_code=payer.issuer_code,
                    amount=payer.mandate_amount,
                    due_date=DUE_DATE,
                    occurred_at=DUE_AT,
                ),
            )
            # Attempt #1: fired by the naive external flow, not MRE (see
            # docs §I.4 / app.ingest's module docstring) — no policy check.
            result = simulator.execute(
                cycle_id=cycle_id,
                sequence_no=1,
                idempotency_key=f"{cycle_id}:seq1:replay",
                mandate_id=mandate_id,
                payer_id=payer.payer_id,
                amount=payer.mandate_amount,
                scheduled_for=DUE_AT,
                issuer_code=payer.issuer_code,
                chronic_fail_propensity=payer.chronic_fail_propensity,
                mean_balance=payer.mean_balance,
                balance_volatility=payer.balance_volatility,
                credit_day=payer.credit_day,
            )
            outcome_event = DebitOutcomeEvent(
                external_id=f"ext:{cycle_id}:attempt1",
                mandate_id=mandate_id,
                cycle_id=cycle_id,
                occurred_at=DUE_AT,
                amount=payer.mandate_amount,
                raw_reason=result.raw_reason,
            )
            if result.outcome == "success":
                ingest_debit_succeeded(conn, outcome_event)
            else:
                ingest_debit_failed(conn, outcome_event)

        print("running P0 fixed-schedule recovery to completion...")
        ticks = 0
        while True:
            row = conn.execute(
                "SELECT MIN(scheduled_for) AS t FROM plan_steps WHERE status = 'pending'"
            ).fetchone()
            assert row is not None
            now = row["t"]
            if now is None:
                break
            process_due_plan_steps(conn, now=now)
            drain_outbox(conn, now=now, simulator=simulator)
            sweep_exhausted_plans(conn, now=now)
            ticks += 1

        recovered = 0
        abandoned = 0
        total_recovered_amount = 0.0
        total_attempts = 0
        for cycle_id in cycle_ids:
            cycle = repo.get_cycle(conn, cycle_id)
            assert cycle is not None
            total_attempts += cycle["attempts_used"]
            if cycle["state"] == "RECOVERED":
                recovered += 1
                total_recovered_amount += float(cycle["recovered_amount"])
            elif cycle["state"] == "ABANDONED":
                abandoned += 1
            else:  # pragma: no cover - would indicate a real bug
                print(f"WARNING: {cycle_id} ended in non-terminal state {cycle['state']}")

    n = len(cycle_ids)
    print()
    print(f"P0 fixed-schedule replay — {n} mandates, {ticks} clock ticks")
    print(f"  recovered:        {recovered} ({100 * recovered / n:.1f}%)")
    print(f"  abandoned:        {abandoned} ({100 * abandoned / n:.1f}%)")
    print(f"  rupees recovered: {total_recovered_amount:,.2f}")
    print(f"  attempts used:    {total_attempts} ({total_attempts / n:.2f} per mandate)")
    if recovered:
        print(f"  attempts per recovery: {total_attempts / recovered:.2f}")


if __name__ == "__main__":
    main()
