from __future__ import annotations

from app.domain.types import CAUSE_DISPOSITION
from app.ml.corpus import ATTEMPT_SEQUENCE_CHOICES, generate_corpus


def test_generate_corpus_is_deterministic() -> None:
    a = generate_corpus("calibration", samples_per_payer=2)
    b = generate_corpus("calibration", samples_per_payer=2)
    assert [(r.snapshot, r.label) for r in a] == [(r.snapshot, r.label) for r in b]


def test_row_count_matches_split_population_times_samples() -> None:
    rows = generate_corpus("calibration", samples_per_payer=3)
    # 15% of 10,000 payers by seed-hash bucketing, not exactly 1,500 -- see
    # data/generator.py's split assignment. Just check the multiple holds.
    assert len(rows) % 3 == 0
    assert len(rows) > 0


def test_only_attempts_2_through_4_are_sampled() -> None:
    rows = generate_corpus("dev", samples_per_payer=5)
    assert {r.snapshot.attempt_sequence_no for r in rows} <= set(ATTEMPT_SEQUENCE_CHOICES)


def test_cause_and_disposition_are_always_consistent() -> None:
    rows = generate_corpus("dev", samples_per_payer=3)
    for r in rows:
        assert CAUSE_DISPOSITION[r.snapshot.cause] == r.snapshot.disposition


def test_labels_are_binary() -> None:
    rows = generate_corpus("dev", samples_per_payer=3)
    assert {r.label for r in rows} <= {0, 1}


def test_timing_signal_is_present_low_balance_days_recover_less_often() -> None:
    """The whole point of simulator/decline.py's timing extension: success
    should correlate with where in the balance cycle a candidate slot
    falls, not just with issuer/chronic-failure attributes."""
    rows = generate_corpus("train", samples_per_payer=6)
    low_balance = [r.label for r in rows if r.snapshot.days_to_credit_day <= 3]
    high_balance = [r.label for r in rows if r.snapshot.days_to_credit_day >= 24]
    assert len(low_balance) > 100 and len(high_balance) > 100
    low_rate = sum(low_balance) / len(low_balance)
    high_rate = sum(high_balance) / len(high_balance)
    assert high_rate > low_rate + 0.05, (
        f"expected recently-credited slots to succeed noticeably more often "
        f"than about-to-be-credited slots: high={high_rate:.3f} low={low_rate:.3f}"
    )
