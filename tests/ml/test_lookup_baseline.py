"""P0b — the deterministic lookup-table baseline (docs T red-team item
2). Pure-function tests on fit_lookup_table/lookup_rate/score_slots_lookup,
plus one test against the real train-split corpus to confirm the fitted
table is sane (matches tests/ml/test_calibrate.py's pattern of one
"real data" test alongside the synthetic ones).
"""
from __future__ import annotations

from datetime import date

from app.domain.types import Cause, MandateSnapshot, RetryDisposition
from app.ml.corpus import CorpusRow, corpus_to_features_and_labels, generate_corpus
from app.ml.lookup_baseline import (
    MIN_BUCKET_SAMPLES,
    MIN_CAUSE_SAMPLES,
    fit_lookup_table,
    lookup_rate,
    score_slots_lookup,
)


def _row(cause: Cause, day_of_month: int, label: int) -> CorpusRow:
    snapshot = MandateSnapshot(
        cause=cause,
        disposition=RetryDisposition.RETRY_TIMING_SENSITIVE,
        attempt_sequence_no=2,
        hours_since_last_failure=24.0,
        day_of_month=day_of_month,
        days_to_credit_day=5,
        slot_of_day=0,
        day_of_week=date(2026, 9, day_of_month).weekday(),
        amount=500,
        amount_over_historical_mean=1.0,
        mandate_age_days=180,
        payer_prior_success_rate=0.7,
        consecutive_prior_failures=0,
        issuer_historical_success_rate=0.85,
        issuer_downtime_active=False,
        rail="upi_autopay",
        segment_proxy="salaried",
        notices_sent_this_cycle=1,
        days_since_last_notice=1.0,
    )
    return CorpusRow(snapshot=snapshot, label=label)


def test_fit_lookup_table_averages_labels_within_a_bucket() -> None:
    rows = [_row(Cause.INSUFFICIENT_FUNDS, 5, 1)] * 15 + [
        _row(Cause.INSUFFICIENT_FUNDS, 5, 0)
    ] * 5
    table = fit_lookup_table(rows)
    assert table.bucket_counts[(Cause.INSUFFICIENT_FUNDS, 5)] == 20
    assert table.by_cause_day[(Cause.INSUFFICIENT_FUNDS, 5)] == 15 / 20


def test_lookup_rate_uses_bucket_when_well_populated() -> None:
    rows = [_row(Cause.INSUFFICIENT_FUNDS, 5, 1)] * MIN_BUCKET_SAMPLES
    table = fit_lookup_table(rows)
    assert lookup_rate(table, cause=Cause.INSUFFICIENT_FUNDS, day_of_month=5) == 1.0


def test_lookup_rate_backs_off_to_cause_level_when_bucket_is_sparse() -> None:
    # Plenty of cause-level data spread over many days, but no single day
    # clears MIN_BUCKET_SAMPLES.
    rows = []
    for day in range(1, 29):
        rows += [_row(Cause.MANDATE_REVOKED, day, 0)] * 3  # 3 < MIN_BUCKET_SAMPLES
    assert len(rows) >= MIN_CAUSE_SAMPLES
    table = fit_lookup_table(rows)
    assert table.bucket_counts[(Cause.MANDATE_REVOKED, 5)] < MIN_BUCKET_SAMPLES
    # All labels are 0, so the cause-level backoff rate is exactly 0.0 —
    # no float tolerance needed for this comparison.
    assert lookup_rate(table, cause=Cause.MANDATE_REVOKED, day_of_month=5) == 0.0
    assert table.by_cause[Cause.MANDATE_REVOKED] == 0.0


def test_lookup_rate_falls_back_to_global_rate_for_a_totally_unseen_cause() -> None:
    rows = [_row(Cause.INSUFFICIENT_FUNDS, d, 1) for d in range(1, 15)]
    table = fit_lookup_table(rows)
    # RISK_DECLINE never appears in the fitted rows at all.
    assert table.cause_counts.get(Cause.RISK_DECLINE, 0) == 0
    assert lookup_rate(table, cause=Cause.RISK_DECLINE, day_of_month=1) == table.global_rate


def test_fit_lookup_table_on_empty_rows_does_not_raise() -> None:
    table = fit_lookup_table([])
    assert table.global_rate == 0.5
    assert lookup_rate(table, cause=Cause.INSUFFICIENT_FUNDS, day_of_month=1) == 0.5


def test_score_slots_lookup_matches_manual_lookup_rate_per_slot() -> None:
    rows = [_row(Cause.INSUFFICIENT_FUNDS, 1, 1)] * MIN_BUCKET_SAMPLES + [
        _row(Cause.INSUFFICIENT_FUNDS, 2, 0)
    ] * MIN_BUCKET_SAMPLES
    table = fit_lookup_table(rows)
    probs = score_slots_lookup(
        table, start_date=date(2026, 9, 1), n_slots=4, cause=Cause.INSUFFICIENT_FUNDS
    )
    # slots 0,1 are day 1 (SLOTS_PER_DAY=2 in app/ml/inference.py), slots 2,3 are day 2.
    assert probs[0] == probs[1] == 1.0
    assert probs[2] == probs[3] == 0.0


def test_lookup_table_fitted_on_real_train_corpus_is_sane() -> None:
    """Not a synthetic fixture — the exact corpus app/ml/train.py fits the
    GBM on, same split discipline (train only, never dev/test)."""
    rows = generate_corpus("train")
    _features, _labels = corpus_to_features_and_labels(rows)  # sanity: same pipeline works
    table = fit_lookup_table(rows)
    assert 0.0 < table.global_rate < 1.0
    # INSUFFICIENT_FUNDS is the benchmark's held-fixed scoring cause and
    # the most common cause in the corpus — every day-of-month bucket for
    # it should be well-populated, not falling back.
    for day in range(1, 29):
        assert table.bucket_counts.get((Cause.INSUFFICIENT_FUNDS, day), 0) >= MIN_BUCKET_SAMPLES
        rate = lookup_rate(table, cause=Cause.INSUFFICIENT_FUNDS, day_of_month=day)
        assert 0.0 <= rate <= 1.0
