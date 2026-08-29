"""Decline-outcome generation: the "reality" the simulator plays.

Uses the same issuer-reliability table as the payer generator
(data/generator.py) so a payer's issuer_code means the same thing on both
sides of the boundary. The simulator returns only a raw string to callers —
never the canonical Cause — because that mirrors what a real payment rail
gives you; normalisation into the 13-value taxonomy is a Phase 6 concern
that belongs to the caller, not to "reality" itself.

Timing sensitivity (docs §J.2): success probability is not just
issuer-reliability x chronic-failure-discount — it also depends on where
in the payer's balance cycle a given day falls. Without this, a success
model trained on this simulator's outputs would learn "issuer predicts
outcome" but nothing about *when* to retry, which would make the planner
(Phase 5) — whose entire value proposition is exploiting timing — have
nothing real to exploit. The timing/balance parameters are optional
kwargs, defaulting to None, so existing callers that don't have payer
context (the Phase 3 worker's outbox dispatch — see docs/ADR/0003) get
unchanged, timing-flat behaviour rather than a silent behavior change.
"""
from __future__ import annotations

import math
import random

from app.domain.types import Cause

from data.generator import ISSUER_SUCCESS_RATES, load_taxonomy, sample_raw_decline

DEFAULT_ISSUER_SUCCESS_RATE = 0.85
DEFAULT_CHRONIC_FAIL_PROPENSITY = 0.08

# The balance-cycle model (docs §DATA_MODEL.md's payer population section):
# a payer's expected balance decays roughly linearly from a peak right
# after `credit_day` down to zero just before the next one. Peak is 2x the
# payer's `mean_balance` so the cycle-average equals `mean_balance` itself
# — keeping that attribute's name honest.
CYCLE_LENGTH_DAYS = 28
BALANCE_PEAK_MULTIPLIER = 2.0


def days_since_credit(day_of_month: int, credit_day: int) -> int:
    return (day_of_month - credit_day) % CYCLE_LENGTH_DAYS


def _expected_balance_fraction(days_since: int) -> float:
    return max(0.0, BALANCE_PEAK_MULTIPLIER * (1.0 - days_since / CYCLE_LENGTH_DAYS))


def funds_sufficiency_probability(
    *, mean_balance: float, balance_volatility: float, days_since: int, amount: float
) -> float:
    """P(payer has >= `amount` available), as a logistic function of the
    expected-balance-to-amount ratio. Higher `balance_volatility` flattens
    the curve — less certain either way, even when the expected balance
    comfortably covers the amount."""
    if amount <= 0:
        return 0.99
    expected_balance = mean_balance * _expected_balance_fraction(days_since)
    ratio = expected_balance / amount
    steepness = 2.5 / max(balance_volatility, 0.1)
    p = 1.0 / (1.0 + math.exp(-steepness * (ratio - 1.0)))
    return min(0.99, max(0.01, p))


# Conditional on failure, which cause produced it. Weighted towards
# INSUFFICIENT_FUNDS per docs §A.3 (the dominant real-world UPI Autopay
# failure mode) and technical declines; NEEDS_HUMAN-disposition causes kept
# rare since they represent genuinely exceptional account states.
FAILURE_CAUSE_WEIGHTS: dict[Cause, float] = {
    Cause.INSUFFICIENT_FUNDS: 0.45,
    Cause.LIMIT_EXCEEDED: 0.05,
    Cause.MANDATE_REVOKED: 0.05,
    Cause.MANDATE_PAUSED: 0.03,
    Cause.ACCOUNT_FROZEN: 0.02,
    Cause.ACCOUNT_CLOSED: 0.02,
    Cause.AFA_REQUIRED: 0.03,
    Cause.ISSUER_TECH_DECLINE: 0.15,
    Cause.PSP_TECH_DECLINE: 0.10,
    Cause.TIMEOUT: 0.05,
    Cause.INVALID_MANDATE_STATE: 0.02,
    Cause.RISK_DECLINE: 0.03,
}

_TAXONOMY = load_taxonomy()


def success_probability(
    issuer_code: str | None,
    chronic_fail_propensity: float | None,
    *,
    mean_balance: float | None = None,
    balance_volatility: float | None = None,
    day_of_month: int | None = None,
    credit_day: int | None = None,
    amount: float | None = None,
) -> float:
    if issuer_code is not None and issuer_code in ISSUER_SUCCESS_RATES:
        issuer_rate = ISSUER_SUCCESS_RATES[issuer_code]
    else:
        issuer_rate = DEFAULT_ISSUER_SUCCESS_RATE
    chronic = (
        chronic_fail_propensity
        if chronic_fail_propensity is not None
        else DEFAULT_CHRONIC_FAIL_PROPENSITY
    )

    have_timing_context = (
        mean_balance is not None
        and balance_volatility is not None
        and day_of_month is not None
        and credit_day is not None
        and amount is not None
    )
    if have_timing_context:
        assert mean_balance is not None
        assert balance_volatility is not None
        assert day_of_month is not None
        assert credit_day is not None
        assert amount is not None
        p_funds = funds_sufficiency_probability(
            mean_balance=mean_balance,
            balance_volatility=balance_volatility,
            days_since=days_since_credit(day_of_month, credit_day),
            amount=amount,
        )
    else:
        p_funds = 1.0  # no payer/timing context available -> flat fallback

    p = p_funds * issuer_rate * (1.0 - chronic)
    return min(0.99, max(0.01, p))


def decide_outcome(
    rng: random.Random,
    *,
    issuer_code: str | None,
    chronic_fail_propensity: float | None,
    mean_balance: float | None = None,
    balance_volatility: float | None = None,
    day_of_month: int | None = None,
    credit_day: int | None = None,
    amount: float | None = None,
) -> tuple[str, str | None]:
    """Returns (outcome, raw_reason). outcome is "success" or "failure"."""
    p = success_probability(
        issuer_code,
        chronic_fail_propensity,
        mean_balance=mean_balance,
        balance_volatility=balance_volatility,
        day_of_month=day_of_month,
        credit_day=credit_day,
        amount=amount,
    )
    if rng.random() < p:
        return "success", None
    causes = list(FAILURE_CAUSE_WEIGHTS)
    weights = list(FAILURE_CAUSE_WEIGHTS.values())
    cause = rng.choices(causes, weights=weights)[0]
    raw, _held_out, _adversarial = sample_raw_decline(rng, cause, _TAXONOMY)
    return "failure", raw
