from __future__ import annotations

from simulator.decline import (
    days_since_credit,
    funds_sufficiency_probability,
    success_probability,
)


def test_days_since_credit_wraps_correctly() -> None:
    assert days_since_credit(day_of_month=1, credit_day=1) == 0
    assert days_since_credit(day_of_month=5, credit_day=1) == 4
    assert days_since_credit(day_of_month=1, credit_day=27) == 2  # wraps past 28


def test_funds_sufficiency_higher_right_after_credit_than_right_before() -> None:
    p_just_credited = funds_sufficiency_probability(
        mean_balance=10_000, balance_volatility=0.5, days_since=0, amount=8_000
    )
    p_just_before_next_credit = funds_sufficiency_probability(
        mean_balance=10_000, balance_volatility=0.5, days_since=27, amount=8_000
    )
    assert p_just_credited > p_just_before_next_credit


def test_higher_volatility_flattens_the_curve() -> None:
    low_vol_gap = funds_sufficiency_probability(
        mean_balance=10_000, balance_volatility=0.1, days_since=0, amount=8_000
    ) - funds_sufficiency_probability(
        mean_balance=10_000, balance_volatility=0.1, days_since=27, amount=8_000
    )
    high_vol_gap = funds_sufficiency_probability(
        mean_balance=10_000, balance_volatility=1.5, days_since=0, amount=8_000
    ) - funds_sufficiency_probability(
        mean_balance=10_000, balance_volatility=1.5, days_since=27, amount=8_000
    )
    assert low_vol_gap > high_vol_gap


def test_success_probability_without_timing_context_is_unchanged_flat_behaviour() -> None:
    p = success_probability("ISS01", 0.0)
    assert p == 0.93  # ISS01's rate * (1 - chronic=0), no timing context -> p_funds=1.0


def test_success_probability_with_timing_context_varies_across_the_month() -> None:
    kwargs = dict(
        issuer_code="ISS01",
        chronic_fail_propensity=0.0,
        mean_balance=10_000.0,
        balance_volatility=0.5,
        credit_day=1,
        amount=8_000.0,
    )
    p_right_after_credit = success_probability(**kwargs, day_of_month=1)
    p_right_before_next_credit = success_probability(**kwargs, day_of_month=28)
    assert p_right_after_credit > p_right_before_next_credit


def test_success_probability_partial_timing_context_falls_back_to_flat() -> None:
    # Missing `amount` -> not enough context -> same as no context at all.
    with_partial = success_probability(
        "ISS01", 0.0, mean_balance=10_000.0, balance_volatility=0.5, day_of_month=1, credit_day=1
    )
    without_any = success_probability("ISS01", 0.0)
    assert with_partial == without_any
