"""Unit tests for the pure, DB-free pieces of evaluation/runner.py (the
Phase 8 paired benchmark). The full pipeline (run_paired_batch) is covered
indirectly by manual `make bench` runs against real Postgres — the same
integration surface tests/integration/test_worker_pipeline.py and
scripts/replay_compare.py already exercise, so it isn't duplicated here.
"""
from __future__ import annotations

from datetime import date, timedelta

from data.generator import Payer
from evaluation.runner import (
    DUE_DATE_SPREAD_DAYS,
    CaseOutcome,
    _amount_metric,
    _due_at_for,
    _due_date_for,
    _recovered_metric,
    bootstrap_ci,
    paired_diffs,
)


def _payer(payer_id: str) -> Payer:
    return Payer(
        payer_id=payer_id,
        segment="salaried",
        credit_day=10,
        mean_balance=5000.0,
        balance_volatility=0.5,
        issuer_code="ISS01",
        chronic_fail_propensity=0.05,
        annoyance_sensitivity=0.3,
        mandate_amount=500.0,
        split="dev",
    )


def test_due_date_for_is_deterministic() -> None:
    p = _payer("PAYER-X")
    assert _due_date_for(p) == _due_date_for(p)


def test_due_date_for_is_within_the_spread_window() -> None:
    p = _payer("PAYER-Y")
    d = _due_date_for(p)
    assert date(2026, 9, 1) <= d < date(2026, 9, 1) + timedelta(days=DUE_DATE_SPREAD_DAYS)


def test_due_date_for_varies_across_payers() -> None:
    # Not a strict guarantee for any two arbitrary IDs, but across a batch
    # of distinct payer_ids the hash-derived offsets should not collapse
    # to a single value -- that's the whole point of this function.
    dates = {_due_date_for(_payer(f"PAYER-{i}")) for i in range(30)}
    assert len(dates) > 1


def test_due_at_for_matches_due_date_for() -> None:
    p = _payer("PAYER-Z")
    assert _due_at_for(p).date() == _due_date_for(p)


def test_paired_diffs_only_over_shared_payers() -> None:
    results = {
        "a": {
            "P1": CaseOutcome(recovered=True, amount=500.0, attempts=1, state="RECOVERED"),
            "P2": CaseOutcome(recovered=False, amount=0.0, attempts=4, state="ABANDONED"),
        },
        "b": {
            "P1": CaseOutcome(recovered=True, amount=500.0, attempts=2, state="RECOVERED"),
            "P3": CaseOutcome(recovered=True, amount=500.0, attempts=1, state="RECOVERED"),
        },
    }
    diffs = paired_diffs(results, "a", "b", _amount_metric)
    assert diffs == [0.0]  # only P1 is shared, and both recovered the same amount


def test_paired_diffs_recovered_metric() -> None:
    results = {
        "a": {"P1": CaseOutcome(recovered=True, amount=500.0, attempts=1, state="RECOVERED")},
        "b": {"P1": CaseOutcome(recovered=False, amount=0.0, attempts=4, state="ABANDONED")},
    }
    diffs = paired_diffs(results, "a", "b", _recovered_metric)
    assert diffs == [1.0]


def test_bootstrap_ci_empty_input_is_zero() -> None:
    assert bootstrap_ci([]) == (0.0, 0.0, 0.0)


def test_bootstrap_ci_all_zero_diffs_gives_a_tight_ci_at_zero() -> None:
    point, lo, hi = bootstrap_ci([0.0] * 50, n_boot=500)
    assert point == 0.0
    assert lo == 0.0
    assert hi == 0.0


def test_bootstrap_ci_reproducible_at_a_fixed_seed() -> None:
    diffs = [1.0, -2.0, 3.0, 0.5, -1.5, 2.5, 4.0, -3.0]
    first = bootstrap_ci(diffs, n_boot=1000, seed=42)
    second = bootstrap_ci(diffs, n_boot=1000, seed=42)
    assert first == second


def test_bootstrap_ci_point_estimate_is_the_sample_mean() -> None:
    diffs = [10.0, 20.0, 30.0]
    point, _lo, _hi = bootstrap_ci(diffs, n_boot=200)
    assert point == 20.0


def test_bootstrap_ci_a_clear_positive_gap_is_significant() -> None:
    diffs = [100.0 + i for i in range(50)]  # all strongly positive, low relative spread
    point, lo, _hi = bootstrap_ci(diffs, n_boot=2000)
    assert point > 0
    assert lo > 0  # CI excludes zero -> "significant" in the runner's own table
