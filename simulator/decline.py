"""Decline-outcome generation: the "reality" the simulator plays.

Uses the same issuer-reliability table as the payer generator
(data/generator.py) so a payer's issuer_code means the same thing on both
sides of the boundary. The simulator returns only a raw string to callers —
never the canonical Cause — because that mirrors what a real payment rail
gives you; normalisation into the 13-value taxonomy is a Phase 6 concern
that belongs to the caller, not to "reality" itself.
"""
from __future__ import annotations

import random

from app.domain.types import Cause

from data.generator import ISSUER_SUCCESS_RATES, load_taxonomy, sample_raw_decline

DEFAULT_ISSUER_SUCCESS_RATE = 0.85
DEFAULT_CHRONIC_FAIL_PROPENSITY = 0.08

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
    issuer_code: str | None, chronic_fail_propensity: float | None
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
    p = issuer_rate * (1.0 - chronic)
    return min(0.99, max(0.02, p))


def decide_outcome(
    rng: random.Random,
    *,
    issuer_code: str | None,
    chronic_fail_propensity: float | None,
) -> tuple[str, str | None]:
    """Returns (outcome, raw_reason). outcome is "success" or "failure"."""
    p = success_probability(issuer_code, chronic_fail_propensity)
    if rng.random() < p:
        return "success", None
    causes = list(FAILURE_CAUSE_WEIGHTS)
    weights = list(FAILURE_CAUSE_WEIGHTS.values())
    cause = rng.choices(causes, weights=weights)[0]
    raw, _held_out, _adversarial = sample_raw_decline(rng, cause, _TAXONOMY)
    return "failure", raw
