"""Phase 8: the rigorous paired benchmark (docs §I.17/§B.3 — explicitly
"never cut", unlike almost everything else in the plan's cut order).

What makes this different from scripts/replay_compare.py's smoke
comparison (independent draws per policy, a directional sanity check
only, dev split only, says so in its own docstring):

* **Same realised world.** Every policy runs against the same batch of
  payers, and the simulator draws outcomes from
  (payer_id, scheduled_for, sequence_no) rather than from each policy's
  own idempotency_key (see simulator/app.py's `_world_seed_key`, added as
  Phase 8 prep). Two policies that attempt on the same real day for the
  same real payer see the identical draw; any observed difference between
  policies is attributable to *when* they chose to act, not to
  independent sampling noise. Attempt 1 is identical across every policy
  by construction (same due date, no policy involved yet).

* **Bootstrap confidence intervals**, not just a point estimate. Each
  payer is its own paired control across policies, so the nonparametric
  bootstrap resamples *payers* (with replacement) and reports the 95% CI
  on the mean per-payer gap between two policies — see `bootstrap_ci`.

* **An oracle policy (P3)**: the same DP solver (app/domain/planner.py)
  that MRE uses, but fed the *true* simulator probability at every slot
  (simulator/decline.py's success_probability — the "reality" function,
  not the trained/calibrated estimate). Nobody could actually run this in
  production — it needs ground truth the real system never has — but it's
  a legitimate upper bound on how much headroom exists to close.

* **A sensitivity sweep** over E_MANUAL (the assumed cost of a manual
  recovery workflow — a genuinely made-up constant, docs §N.7 flags it as
  exactly this kind of unmeasured assumption) across 3 parameterizations,
  to check that any observed ranking isn't an artifact of one arbitrary
  number.

Split discipline (docs §J.5/§T): every default here is `dev`. The sealed
`test` split is for the one, final, locked run only — `--split test` is
never the default and should be invoked by hand, once, after everything
else about this module is already trusted on `dev`.

Usage: `make bench` (dev, the default and the safe repeatable command).
For the one final sealed run: `.venv/bin/python -m evaluation.runner
--split test --sensitivity`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import numpy as np
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
from app.ml.inference import PayerContext, score_slots, slot_datetime
from app.ml.lookup_baseline import LookupTable, fit_lookup_table, score_slots_lookup
from app.ml.registry import ModelArtifact
from app.ml.train import fit_success_model
from app.policies.greedy import POLICY_VERSION as GREEDY_VERSION
from app.policies.greedy import compute_greedy_schedule
from app.policies.mre import POLICY_VERSION as MRE_VERSION
from app.policies.mre import compute_mre_schedule
from app.workflows.worker import drain_outbox, process_due_plan_steps, sweep_exhausted_plans

from data.generator import Payer, generate_population
from simulator.app import create_app
from simulator.decline import success_probability

N_MANDATES_DEFAULT = 300
ANCHOR_DATE = date(2026, 9, 1)
DUE_HOUR = 2
N_SLOTS = 28
DUE_DATE_SPREAD_DAYS = 28  # see _due_date_for


def _due_date_for(payer: Payer) -> date:
    """Real recurring-payment due dates are spread across the month, not
    clustered on one day — sharing a single due_date (as
    scripts/replay_fixed.py's own docstring already flags: "A realistic
    spread of due dates is Phase 8 scope") means every payer's 3 remaining
    attempts fall in the *same* 14-day calendar window relative to
    `due_date`, which structurally caps how much timing skill can matter
    in aggregate — for a payer whose credit_day has just passed relative
    to that shared window, no policy, however smart, can reach it before
    the NPCI 4-attempt cap forces escalation. Deterministic per payer_id
    (not `data.generator`'s own RNG, to avoid coupling to its internal
    sequence) so the batch is reproducible at a fixed seed."""
    digest = hashlib.sha256(f"due_date:{payer.payer_id}".encode()).digest()
    offset_days = int.from_bytes(digest[:4], "big") % DUE_DATE_SPREAD_DAYS
    return ANCHOR_DATE + timedelta(days=offset_days)


def _due_at_for(payer: Payer) -> datetime:
    return datetime.combine(_due_date_for(payer), dt_time(DUE_HOUR, tzinfo=UTC))


E_MANUAL_DEFAULT = 150.0
E_MANUAL_LATE_RATIO = 30.0 / 150.0  # keeps the fixed/greedy/mre ratio used elsewhere
SCORING_CAUSE = Cause.INSUFFICIENT_FUNDS  # see replay_compare.py's docstring — same reasoning
POLICIES = ("fixed", "greedy", "lookup", "mre", "oracle")
ORACLE_POLICY_VERSION = "ORACLE-dp-v1"
LOOKUP_POLICY_VERSION = "P0b-lookup-v1"

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
    """Always train/calibration split, regardless of which split is being
    *evaluated* — the model must never see dev or test."""
    train_rows = generate_corpus("train")
    train_features, train_labels = corpus_to_features_and_labels(train_rows)
    model, encoder = fit_success_model(train_features, train_labels)

    calib_rows = generate_corpus("calibration")
    calib_features, calib_labels = corpus_to_features_and_labels(calib_rows)
    isotonic = fit_isotonic(model, encoder, calib_features, calib_labels)

    return ModelArtifact(model=model, encoder=encoder, isotonic=isotonic, version="bench")


def _train_lookup_table() -> LookupTable:
    """P0b (docs §T red-team item 2): fit from the identical `train`-split
    corpus the GBM trains on — same split discipline, same input data —
    so the comparison to `mre`/`greedy` isolates what the calibrated model
    is worth over a plain (cause, day-of-month) average, not a difference
    in what data each baseline got to see."""
    return fit_lookup_table(generate_corpus("train"))


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
        # is held fixed for a fair aggregate timing comparison, unlike
        # app/policies/live.py's cause-aware production wiring.
        probs = score_slots(
            artifact, payer=_payer_context(payer), start_date=due_date, n_slots=N_SLOTS,
            attempt_sequence_no=2, cause=SCORING_CAUSE, consecutive_prior_failures=0,
        )
        config = PlannerConfig(n_slots=N_SLOTS, max_attempts=4)
        inputs = PlanningInputs(
            amount=payer.mandate_amount, p_success=probs,
            e_manual=E_MANUAL_DEFAULT, e_manual_late=E_MANUAL_DEFAULT * E_MANUAL_LATE_RATIO,
        )
        plan = compute_greedy_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version=GREEDY_VERSION, steps=plan.steps, immediate_stop=plan.immediate_stop
        )

    return compute_plan


def _lookup_compute_plan(
    table: LookupTable, payer: Payer
) -> Callable[[date, Cause], PlanChoice]:
    """P0b — see app/ml/lookup_baseline.py's module docstring. Identical
    naive-greedy scheduling logic to `greedy` (compute_greedy_schedule),
    fed a plain (cause, day-of-month) table lookup instead of the trained,
    calibrated model. The only variable this isolates relative to
    `greedy` is the source of P(success); the DP-vs-greedy comparison
    (mre vs greedy) is a separate axis."""

    def compute_plan(due_date: date, _cause: Cause) -> PlanChoice:
        # _cause ignored on purpose -- see _greedy_compute_plan above.
        probs = score_slots_lookup(
            table, start_date=due_date, n_slots=N_SLOTS, cause=SCORING_CAUSE
        )
        config = PlannerConfig(n_slots=N_SLOTS, max_attempts=4)
        inputs = PlanningInputs(
            amount=payer.mandate_amount, p_success=probs,
            e_manual=E_MANUAL_DEFAULT, e_manual_late=E_MANUAL_DEFAULT * E_MANUAL_LATE_RATIO,
        )
        plan = compute_greedy_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version=LOOKUP_POLICY_VERSION, steps=plan.steps,
            immediate_stop=plan.immediate_stop,
        )

    return compute_plan


def _mre_compute_plan(
    artifact: ModelArtifact, payer: Payer, *, e_manual: float
) -> Callable[[date, Cause], PlanChoice]:
    def compute_plan(due_date: date, _cause: Cause) -> PlanChoice:
        # _cause ignored on purpose -- see _greedy_compute_plan above.
        probs = score_slots(
            artifact, payer=_payer_context(payer), start_date=due_date, n_slots=N_SLOTS,
            attempt_sequence_no=2, cause=SCORING_CAUSE, consecutive_prior_failures=0,
        )
        config = PlannerConfig(n_slots=N_SLOTS, max_attempts=4)
        inputs = PlanningInputs(
            amount=payer.mandate_amount, p_success=probs,
            e_manual=e_manual, e_manual_late=e_manual * E_MANUAL_LATE_RATIO,
        )
        plan = compute_mre_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version=MRE_VERSION, steps=plan.steps, immediate_stop=plan.immediate_stop,
            expected_value=plan.expected_value, solver_ms=plan.solver_ms,
        )

    return compute_plan


def _oracle_compute_plan(
    payer: Payer, *, e_manual: float
) -> Callable[[date, Cause], PlanChoice]:
    """The perfect-information ceiling: the identical DP solver MRE uses,
    fed simulator/decline.py's true success_probability at every slot
    instead of the trained model's calibrated estimate. Not a policy the
    real system could run (it needs ground truth); a bound on how much
    headroom MRE's model-based estimate leaves on the table."""

    def compute_plan(due_date: date, _cause: Cause) -> PlanChoice:
        # _cause ignored -- the oracle already has strictly more ground
        # truth than the real cause could add (see docstring above).
        probs = tuple(
            success_probability(
                payer.issuer_code,
                payer.chronic_fail_propensity,
                mean_balance=payer.mean_balance,
                balance_volatility=payer.balance_volatility,
                day_of_month=slot_datetime(due_date, t).day,
                credit_day=payer.credit_day,
                amount=payer.mandate_amount,
            )
            for t in range(N_SLOTS)
        )
        config = PlannerConfig(n_slots=N_SLOTS, max_attempts=4)
        inputs = PlanningInputs(
            amount=payer.mandate_amount, p_success=probs,
            e_manual=e_manual, e_manual_late=e_manual * E_MANUAL_LATE_RATIO,
        )
        plan = compute_mre_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version=ORACLE_POLICY_VERSION, steps=plan.steps,
            immediate_stop=plan.immediate_stop, expected_value=plan.expected_value,
            solver_ms=plan.solver_ms,
        )

    return compute_plan


@dataclass(frozen=True)
class CaseOutcome:
    recovered: bool
    amount: float
    attempts: int
    state: str


def _seed_and_run_attempt_one(
    conn: Conn,
    simulator: SimulatorClient,
    artifact: ModelArtifact,
    lookup_table: LookupTable,
    policy: str,
    payer: Payer,
    cycle_id: str,
    mandate_id: str,
    *,
    e_manual: float,
) -> None:
    due_date = _due_date_for(payer)
    due_at = _due_at_for(payer)
    ingest_cycle_due(
        conn,
        CycleDueEvent(
            external_id=f"ext:{cycle_id}:due", mandate_id=mandate_id, cycle_id=cycle_id,
            merchant_id="MERCH-BENCH", payer_id=payer.payer_id, rail="upi_autopay",
            issuer_code=payer.issuer_code, amount=payer.mandate_amount, due_date=due_date,
            occurred_at=due_at,
        ),
    )
    # Attempt 1 is identical across every policy by construction: same
    # payer, same due date (per-payer, see _due_date_for), no policy
    # involved yet (see module docstring and app.ingest's "attempt #1 is
    # external" design note) — and thanks to the shared-world-seed fix,
    # it's now also the literal same draw.
    result = simulator.execute(
        cycle_id=cycle_id, sequence_no=1, idempotency_key=f"{cycle_id}:seq1:bench",
        mandate_id=mandate_id, payer_id=payer.payer_id, amount=payer.mandate_amount,
        scheduled_for=due_at, issuer_code=payer.issuer_code,
        chronic_fail_propensity=payer.chronic_fail_propensity, mean_balance=payer.mean_balance,
        balance_volatility=payer.balance_volatility, credit_day=payer.credit_day,
    )
    outcome_event = DebitOutcomeEvent(
        external_id=f"ext:{cycle_id}:attempt1", mandate_id=mandate_id, cycle_id=cycle_id,
        occurred_at=due_at, amount=payer.mandate_amount, raw_reason=result.raw_reason,
    )
    if result.outcome == "success":
        ingest_debit_succeeded(conn, outcome_event)
        return

    if policy == "fixed":
        ingest_debit_failed(conn, outcome_event)
    elif policy == "greedy":
        ingest_debit_failed(conn, outcome_event, compute_plan=_greedy_compute_plan(artifact, payer))
    elif policy == "lookup":
        ingest_debit_failed(
            conn, outcome_event, compute_plan=_lookup_compute_plan(lookup_table, payer)
        )
    elif policy == "mre":
        ingest_debit_failed(
            conn, outcome_event, compute_plan=_mre_compute_plan(artifact, payer, e_manual=e_manual)
        )
    elif policy == "oracle":
        ingest_debit_failed(
            conn, outcome_event, compute_plan=_oracle_compute_plan(payer, e_manual=e_manual)
        )
    else:  # pragma: no cover - exhaustive over POLICIES
        raise AssertionError(f"unhandled policy {policy}")


def run_paired_batch(
    conn: Conn,
    simulator: SimulatorClient,
    artifact: ModelArtifact,
    lookup_table: LookupTable,
    payers: list[Payer],
    policies: Sequence[str] = POLICIES,
    *,
    e_manual: float = E_MANUAL_DEFAULT,
) -> dict[str, dict[str, CaseOutcome]]:
    """Runs every policy in `policies` against the same `payers`, in the
    shared realised world. Returns {policy: {payer_id: CaseOutcome}}."""
    conn.execute(f"TRUNCATE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE")
    conn.commit()

    cycle_ids_by_policy: dict[str, list[tuple[str, str]]] = {p: [] for p in policies}
    for policy in policies:
        for payer in payers:
            cycle_id = f"CYC-{policy}-{payer.payer_id}"
            mandate_id = f"MANDATE-{policy}-{payer.payer_id}"
            cycle_ids_by_policy[policy].append((cycle_id, payer.payer_id))
            _seed_and_run_attempt_one(
                conn, simulator, artifact, lookup_table, policy, payer, cycle_id, mandate_id,
                e_manual=e_manual,
            )

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

    results: dict[str, dict[str, CaseOutcome]] = {p: {} for p in policies}
    for policy in policies:
        for cycle_id, payer_id in cycle_ids_by_policy[policy]:
            cycle = repo.get_cycle(conn, cycle_id)
            assert cycle is not None
            results[policy][payer_id] = CaseOutcome(
                recovered=cycle["state"] == "RECOVERED",
                amount=float(cycle["recovered_amount"]),
                attempts=int(cycle["attempts_used"]),
                state=cycle["state"],
            )
    return results


def paired_diffs(
    results: dict[str, dict[str, CaseOutcome]],
    policy_a: str,
    policy_b: str,
    metric: Callable[[CaseOutcome], float],
) -> list[float]:
    """metric(a) - metric(b) for every payer both policies ran, in a fixed
    order — the raw material for the paired bootstrap."""
    a, b = results[policy_a], results[policy_b]
    shared_payers = [pid for pid in a if pid in b]
    return [metric(a[pid]) - metric(b[pid]) for pid in shared_payers]


def bootstrap_ci(
    diffs: Sequence[float], *, n_boot: int = 5000, alpha: float = 0.05, seed: int = 20260905
) -> tuple[float, float, float]:
    """Nonparametric paired bootstrap on a list of per-payer differences.
    Returns (point_estimate, ci_lo, ci_hi)."""
    arr = np.asarray(diffs, dtype=float)
    n = len(arr)
    if n == 0:
        return (0.0, 0.0, 0.0)
    point = float(arr.mean())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    boot_means = arr[idx].mean(axis=1)
    lo, hi = np.percentile(boot_means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return (point, float(lo), float(hi))


def _amount_metric(o: CaseOutcome) -> float:
    return o.amount


def _recovered_metric(o: CaseOutcome) -> float:
    return 1.0 if o.recovered else 0.0


def _summary_rows(
    results: dict[str, dict[str, CaseOutcome]], policies: Sequence[str]
) -> list[dict[str, Any]]:
    n = len(next(iter(results.values())))
    rows = []
    for policy in policies:
        outcomes = results[policy].values()
        recovered = sum(1 for o in outcomes if o.recovered)
        rupees = sum(o.amount for o in outcomes)
        attempts = sum(o.attempts for o in outcomes)
        rows.append(
            {
                "policy": policy,
                "n": n,
                "recovered": recovered,
                "recovery_rate": recovered / n if n else 0.0,
                "rupees_recovered": rupees,
                "attempts_used": attempts,
            }
        )
    return rows


def _paired_comparison_rows(
    results: dict[str, dict[str, CaseOutcome]], policies: Sequence[str], *, n_boot: int
) -> list[dict[str, Any]]:
    rows = []
    for i, a in enumerate(policies):
        for b in policies[i + 1 :]:
            diffs = paired_diffs(results, a, b, _amount_metric)
            point, lo, hi = bootstrap_ci(diffs, n_boot=n_boot)
            rows.append(
                {
                    "a": a,
                    "b": b,
                    "mean_gap_rupees_per_payer": point,
                    "ci_lo": lo,
                    "ci_hi": hi,
                    "significant": bool(lo > 0 or hi < 0),
                }
            )
    return rows


def write_report(
    results: dict[str, dict[str, CaseOutcome]],
    policies: Sequence[str],
    *,
    split: str,
    n_boot: int,
    out_dir: Path,
) -> None:
    """docs §S.2: 'reports/BENCHMARK.md generated by script, not by hand.'
    Writes both the Markdown (for the README/submission) and a JSON
    snapshot (for the dashboard's benchmark screen) from the same data."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = _summary_rows(results, policies)
    comparisons = _paired_comparison_rows(results, policies, n_boot=n_boot)
    n = summary[0]["n"] if summary else 0

    payload = {
        "split": split,
        "n_mandates": n,
        "n_boot": n_boot,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "paired_comparisons": comparisons,
    }
    (out_dir / "benchmark.json").write_text(json.dumps(payload, indent=2) + "\n")

    lines = [
        "# Benchmark report",
        "",
        f"Generated by `evaluation/runner.py` — not written by hand. Split: `{split}`, "
        f"n={n}, bootstrap resamples={n_boot}.",
        "",
        "## Summary",
        "",
        "| policy | recovered | rate | rupees | attempts |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in summary:
        lines.append(
            f"| {row['policy']} | {row['recovered']} | {100 * row['recovery_rate']:.1f}% | "
            f"{row['rupees_recovered']:,.2f} | {row['attempts_used']} |"
        )
    lines += [
        "",
        "## Paired bootstrap 95% CI on mean per-payer rupee gap (A − B)",
        "",
        "| A | B | mean gap | ci_lo | ci_hi | significant |",
        "|---|---|---:|---:|---:|:---:|",
    ]
    for row in comparisons:
        lines.append(
            f"| {row['a']} | {row['b']} | {row['mean_gap_rupees_per_payer']:,.2f} | "
            f"{row['ci_lo']:,.2f} | {row['ci_hi']:,.2f} | "
            f"{'yes' if row['significant'] else 'no'} |"
        )
    lines += [
        "",
        "Whatever the numbers say, they are the numbers (docs §I.15). See CLAUDE.md's "
        "Phase 8 notes for the full investigation of *why* they came out this way, and "
        "README.md's Evaluation section for the honesty paragraph.",
        "",
    ]
    (out_dir / "BENCHMARK.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {out_dir / 'BENCHMARK.md'} and {out_dir / 'benchmark.json'}")


def print_summary_table(
    results: dict[str, dict[str, CaseOutcome]], policies: Sequence[str]
) -> None:
    n = len(next(iter(results.values())))
    print(f"{'policy':<8} {'recovered':>10} {'rate':>7} {'rupees':>14} {'attempts':>9}")
    for policy in policies:
        outcomes = results[policy].values()
        recovered = sum(1 for o in outcomes if o.recovered)
        rupees = sum(o.amount for o in outcomes)
        attempts = sum(o.attempts for o in outcomes)
        print(
            f"{policy:<8} {recovered:>10} {100 * recovered / n:>6.1f}% "
            f"{rupees:>14,.2f} {attempts:>9}"
        )


def print_paired_comparisons(
    results: dict[str, dict[str, CaseOutcome]], policies: Sequence[str], *, n_boot: int
) -> None:
    print()
    print("paired bootstrap 95% CI on mean per-payer gap (rupees recovered, A - B):")
    print(f"{'A':<8} {'B':<8} {'mean gap':>12} {'ci_lo':>12} {'ci_hi':>12} {'significant':>12}")
    for i, a in enumerate(policies):
        for b in policies[i + 1 :]:
            diffs = paired_diffs(results, a, b, _amount_metric)
            point, lo, hi = bootstrap_ci(diffs, n_boot=n_boot)
            significant = "yes" if (lo > 0) or (hi < 0) else "no"
            print(f"{a:<8} {b:<8} {point:>12,.2f} {lo:>12,.2f} {hi:>12,.2f} {significant:>12}")


def run_sensitivity_sweep(
    conn: Conn,
    simulator: SimulatorClient,
    artifact: ModelArtifact,
    lookup_table: LookupTable,
    payers: list[Payer],
    *,
    e_manual_values: Sequence[float] = (100.0, 150.0, 250.0),
    n_boot: int,
) -> None:
    print()
    print("=" * 78)
    print("sensitivity sweep: E_MANUAL (assumed manual-recovery cost, docs §N.7)")
    print("=" * 78)
    for e_manual in e_manual_values:
        e_manual_late = e_manual * E_MANUAL_LATE_RATIO
        print(f"\n--- E_MANUAL = {e_manual:.2f} (E_MANUAL_LATE = {e_manual_late:.2f}) ---")
        results = run_paired_batch(
            conn, simulator, artifact, lookup_table, payers, POLICIES, e_manual=e_manual
        )
        print_summary_table(results, POLICIES)
        diffs = paired_diffs(results, "mre", "fixed", _amount_metric)
        point, lo, hi = bootstrap_ci(diffs, n_boot=n_boot)
        print(f"  mre - fixed rupee gap: {point:,.2f}  95% CI [{lo:,.2f}, {hi:,.2f}]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="dev", choices=("dev", "test"))
    parser.add_argument("--n", type=int, default=N_MANDATES_DEFAULT)
    parser.add_argument("--n-boot", type=int, default=5000)
    parser.add_argument("--sensitivity", action="store_true")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="defaults to reports/ for --split test (the one locked submission "
        "artifact) and reports/dev/ for --split dev, so a routine dev-split run "
        "can never silently overwrite the locked test-split report (docs "
        "ENGINEERING_LOG.md's P0b entry: this exact thing happened once, caught "
        "only by git status before it was committed)",
    )
    parser.add_argument("--no-report", action="store_true", help="skip writing reports/")
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = Path("reports") if args.split == "test" else Path("reports/dev")

    if args.split == "test":
        print(
            "!! Running against the SEALED test split. This split is meant to be "
            "touched exactly once (docs §J.5/§T). Make sure this is the final, "
            "locked run — Ctrl-C now if it isn't."
        )

    print("training the success model (train + calibration splits, never "
          f"{args.split})...")
    artifact = _train_artifact()
    print("fitting the P0b lookup-table baseline (train split, docs §T item 2)...")
    lookup_table = _train_lookup_table()

    print("starting in-process simulator...")
    base_url = _start_simulator()
    simulator = SimulatorClient(base_url=base_url)

    payers = [p for p in generate_population() if p.split == args.split][: args.n]
    print(f"seeding {len(payers)} mandates x {len(POLICIES)} policies ({args.split} split)...")

    with get_connection() as conn:
        results = run_paired_batch(conn, simulator, artifact, lookup_table, payers, POLICIES)

        print()
        print(
            f"paired benchmark — {len(payers)} mandates, {args.split} split, "
            "shared realised world"
        )
        print_summary_table(results, POLICIES)
        print_paired_comparisons(results, POLICIES, n_boot=args.n_boot)

        if not args.no_report:
            write_report(
                results, POLICIES, split=args.split, n_boot=args.n_boot, out_dir=args.out_dir
            )

        if args.sensitivity:
            run_sensitivity_sweep(
                conn, simulator, artifact, lookup_table, payers, n_boot=args.n_boot
            )


if __name__ == "__main__":
    main()
