"""Phase 5 smoke comparison: P0 `fixed` vs `greedy` vs `mre` over the same
N synthetic mandates, end to end through the real ingestion -> worker ->
outbox -> simulator path.

NOT the rigorous paired Day-5 benchmark (evaluation/runner.py, Phase 8:
same seeds, same realised world, bootstrap CIs, an oracle ceiling). Each
policy gets its own cycle_id per payer and therefore its own independent
draws from the simulator — not a shared realised world — so treat this as
a directional sanity check ("does MRE beat the baselines in aggregate on
a reasonably large batch"), not a rigorous causal comparison. That
rigor is Phase 8's job, deliberately deferred, not skipped.

Also deferred: Phase 6's decline-string normaliser doesn't exist yet, so
every case is scored as if cause=INSUFFICIENT_FUNDS (the dominant real
UPI Autopay failure mode per docs §A.3) regardless of the simulator's
actual raw decline string. Fine for judging the *planner's* timing value
in aggregate; would need the real normaliser for per-case accuracy.

Draws from `dev` only — the sealed `test` split stays untouched (docs
§J.5 / §T red-team point 3).

Usage: `make replay-compare`.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

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
from app.ml.calibrate import fit_isotonic
from app.ml.corpus import corpus_to_features_and_labels, generate_corpus
from app.ml.inference import PayerContext, score_slots
from app.ml.registry import ModelArtifact
from app.ml.train import fit_success_model
from app.policies.greedy import POLICY_VERSION as GREEDY_VERSION
from app.policies.greedy import compute_greedy_schedule
from app.policies.mre import POLICY_VERSION as MRE_VERSION
from app.policies.mre import compute_mre_schedule
from app.workflows.worker import drain_outbox, process_due_plan_steps, sweep_exhausted_plans

from data.generator import Payer, generate_population
from simulator.app import create_app

N_MANDATES = 300
DUE_DATE = date(2026, 9, 1)
DUE_AT = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
N_SLOTS = 28
E_MANUAL = 150.0
E_MANUAL_LATE = 30.0
SCORING_CAUSE = Cause.INSUFFICIENT_FUNDS  # see module docstring
POLICIES = ("fixed", "greedy", "mre")

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


def _train_artifact() -> ModelArtifact:
    train_rows = generate_corpus("train")
    train_features, train_labels = corpus_to_features_and_labels(train_rows)
    model, encoder = fit_success_model(train_features, train_labels)

    calib_rows = generate_corpus("calibration")
    calib_features, calib_labels = corpus_to_features_and_labels(calib_rows)
    isotonic = fit_isotonic(model, encoder, calib_features, calib_labels)

    return ModelArtifact(model=model, encoder=encoder, isotonic=isotonic, version="replay-compare")


def _payer_context(p: Payer) -> PayerContext:
    return PayerContext(
        payer_id=p.payer_id,
        segment=p.segment,
        credit_day=p.credit_day,
        mean_balance=p.mean_balance,
        balance_volatility=p.balance_volatility,
        issuer_code=p.issuer_code,
        chronic_fail_propensity=p.chronic_fail_propensity,
        mandate_amount=p.mandate_amount,
    )


def _greedy_compute_plan(
    artifact: ModelArtifact, payer: Payer
) -> Callable[[date, Cause], PlanChoice]:
    def compute_plan(due_date: date, _cause: Cause) -> PlanChoice:
        # _cause ignored on purpose -- see module docstring: SCORING_CAUSE
        # is held fixed for a fair aggregate timing comparison across
        # policies, independently of the simulator's real decline string.
        probs = score_slots(
            artifact, payer=_payer_context(payer), start_date=due_date, n_slots=N_SLOTS,
            attempt_sequence_no=2, cause=SCORING_CAUSE, consecutive_prior_failures=0,
        )
        config = PlannerConfig(n_slots=N_SLOTS, max_attempts=4)
        inputs = PlanningInputs(
            amount=payer.mandate_amount, p_success=probs, e_manual=E_MANUAL,
            e_manual_late=E_MANUAL_LATE,
        )
        plan = compute_greedy_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version=GREEDY_VERSION, steps=plan.steps, immediate_stop=plan.immediate_stop
        )

    return compute_plan


def _mre_compute_plan(
    artifact: ModelArtifact, payer: Payer
) -> Callable[[date, Cause], PlanChoice]:
    def compute_plan(due_date: date, _cause: Cause) -> PlanChoice:
        # _cause ignored on purpose -- see _greedy_compute_plan above.
        probs = score_slots(
            artifact, payer=_payer_context(payer), start_date=due_date, n_slots=N_SLOTS,
            attempt_sequence_no=2, cause=SCORING_CAUSE, consecutive_prior_failures=0,
        )
        config = PlannerConfig(n_slots=N_SLOTS, max_attempts=4)
        inputs = PlanningInputs(
            amount=payer.mandate_amount, p_success=probs, e_manual=E_MANUAL,
            e_manual_late=E_MANUAL_LATE,
        )
        plan = compute_mre_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version=MRE_VERSION, steps=plan.steps, immediate_stop=plan.immediate_stop,
            expected_value=plan.expected_value, solver_ms=plan.solver_ms,
        )

    return compute_plan


@dataclass
class PolicyStats:
    recovered: int = 0
    awaiting_manual: int = 0
    abandoned: int = 0
    total_recovered_amount: float = 0.0
    total_attempts: int = 0


def main() -> None:
    print("training the success model (train + calibration splits)...")
    artifact = _train_artifact()

    print("starting in-process simulator...")
    base_url = _start_simulator()
    simulator = SimulatorClient(base_url=base_url)

    payers = [p for p in generate_population() if p.split == "dev"][:N_MANDATES]
    print(f"seeding {len(payers)} mandates x {len(POLICIES)} policies (dev split only)...")

    cycle_ids_by_policy: dict[str, list[str]] = {policy: [] for policy in POLICIES}

    with get_connection() as conn:
        conn.execute(f"TRUNCATE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()

        for policy in POLICIES:
            for payer in payers:
                cycle_id = f"CYC-{policy}-{payer.payer_id}"
                mandate_id = f"MANDATE-{policy}-{payer.payer_id}"
                cycle_ids_by_policy[policy].append(cycle_id)
                _seed_and_run_attempt_one(
                    conn, simulator, artifact, policy, payer, cycle_id, mandate_id
                )

        print("running recovery to completion across all policies...")
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

        stats: dict[str, PolicyStats] = {policy: PolicyStats() for policy in POLICIES}
        for policy in POLICIES:
            for cycle_id in cycle_ids_by_policy[policy]:
                cycle = repo.get_cycle(conn, cycle_id)
                assert cycle is not None
                s = stats[policy]
                s.total_attempts += cycle["attempts_used"]
                if cycle["state"] == "RECOVERED":
                    s.recovered += 1
                    s.total_recovered_amount += float(cycle["recovered_amount"])
                elif cycle["state"] == "AWAITING_MANUAL":
                    s.awaiting_manual += 1
                elif cycle["state"] == "ABANDONED":
                    s.abandoned += 1
                else:  # pragma: no cover
                    print(f"WARNING: {cycle_id} ended in non-terminal state {cycle['state']}")

    n = len(payers)
    print()
    print(f"{'policy':<8} {'recovered':>10} {'awaiting_mgr':>13} {'abandoned':>10} "
          f"{'rupees':>14} {'attempts':>9} {'attempts/recovery':>18}")
    for policy in POLICIES:
        s = stats[policy]
        per_recovery = s.total_attempts / s.recovered if s.recovered else float("nan")
        print(
            f"{policy:<8} {s.recovered:>10} {s.awaiting_manual:>13} {s.abandoned:>10} "
            f"{s.total_recovered_amount:>14,.2f} {s.total_attempts:>9} {per_recovery:>18.2f}"
        )
    print()
    print(f"({n} mandates per policy, {ticks} clock ticks, dev split, not a paired comparison)")


def _seed_and_run_attempt_one(
    conn: Conn,
    simulator: SimulatorClient,
    artifact: ModelArtifact,
    policy: str,
    payer: Payer,
    cycle_id: str,
    mandate_id: str,
) -> None:
    ingest_cycle_due(
        conn,
        CycleDueEvent(
            external_id=f"ext:{cycle_id}:due",
            mandate_id=mandate_id,
            cycle_id=cycle_id,
            merchant_id="MERCH-COMPARE",
            payer_id=payer.payer_id,
            rail="upi_autopay",
            issuer_code=payer.issuer_code,
            amount=payer.mandate_amount,
            due_date=DUE_DATE,
            occurred_at=DUE_AT,
        ),
    )
    result = simulator.execute(
        cycle_id=cycle_id,
        sequence_no=1,
        idempotency_key=f"{cycle_id}:seq1:compare",
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
        return

    if policy == "fixed":
        ingest_debit_failed(conn, outcome_event)
    elif policy == "greedy":
        ingest_debit_failed(
            conn, outcome_event, compute_plan=_greedy_compute_plan(artifact, payer)
        )
    elif policy == "mre":
        ingest_debit_failed(conn, outcome_event, compute_plan=_mre_compute_plan(artifact, payer))
    else:  # pragma: no cover - exhaustive over POLICIES
        raise AssertionError(f"unhandled policy {policy}")


if __name__ == "__main__":
    main()
