"""Score candidate slots for a real cycle using a trained Phase 4
artifact + a real (persisted) payer's attributes — the bridge between
ml/ (§K.3's "scorer") and the planner (§K.4), kept as a separate
component from both on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from app.domain.types import CAUSE_DISPOSITION, Cause, MandateSnapshot
from app.ml.features import assemble_features
from app.ml.registry import ModelArtifact
from data.generator import ISSUER_SUCCESS_RATES

SLOTS_PER_DAY = 2
SLOT_HOURS = (2, 23)  # matches simulator's permitted (non-peak) window


@dataclass(frozen=True)
class PayerContext:
    payer_id: str
    segment: str
    credit_day: int
    mean_balance: float
    balance_volatility: float
    issuer_code: str
    chronic_fail_propensity: float
    mandate_amount: float


def payer_context_from_row(row: dict[str, Any]) -> PayerContext:
    return PayerContext(
        payer_id=str(row["id"]),
        segment=str(row["segment"]),
        credit_day=int(row["credit_day"]),
        mean_balance=float(row["mean_balance"]),
        balance_volatility=float(row["balance_volatility"]),
        issuer_code=str(row["issuer_code"]),
        chronic_fail_propensity=float(row["chronic_fail_propensity"]),
        mandate_amount=float(row["mandate_amount"]),
    )


def slot_datetime(start_date: date, slot_index: int) -> datetime:
    day_offset, hour_idx = divmod(slot_index, SLOTS_PER_DAY)
    day = start_date + timedelta(days=day_offset)
    hour = SLOT_HOURS[hour_idx]
    return datetime.combine(day, time(hour, tzinfo=UTC))


def score_slots(
    artifact: ModelArtifact,
    *,
    payer: PayerContext,
    start_date: date,
    n_slots: int,
    attempt_sequence_no: int,
    cause: Cause,
    consecutive_prior_failures: int,
    notices_sent_this_cycle: int = 0,
) -> tuple[float, ...]:
    """Calibrated P(success) for an ATTEMPT at each slot t in [0, n_slots).

    Simplification, documented rather than hidden: attempt_sequence_no,
    cause and consecutive_prior_failures are held fixed across the whole
    horizon for this scoring pass, even though a real adaptive plan's
    actual sequence number and failure history would change slot to slot.
    This matches app/policies/mre.py's own pre-committed-schedule
    simplification (see its module docstring) — both stem from the same
    scope boundary: full dynamic re-planning is Phase 7+ territory.
    """
    disposition = CAUSE_DISPOSITION[cause]
    issuer_rate = ISSUER_SUCCESS_RATES.get(payer.issuer_code, 0.85)
    rows = []
    for t in range(n_slots):
        dt = slot_datetime(start_date, t)
        rows.append(
            assemble_features(
                MandateSnapshot(
                    cause=cause,
                    disposition=disposition,
                    attempt_sequence_no=attempt_sequence_no,
                    hours_since_last_failure=24.0,
                    day_of_month=dt.day,
                    days_to_credit_day=(payer.credit_day - dt.day) % 28,
                    slot_of_day=0 if dt.hour < 12 else 1,
                    day_of_week=dt.weekday(),
                    amount=int(round(payer.mandate_amount)),
                    amount_over_historical_mean=1.0,
                    mandate_age_days=180,
                    payer_prior_success_rate=issuer_rate,
                    consecutive_prior_failures=consecutive_prior_failures,
                    issuer_historical_success_rate=issuer_rate,
                    issuer_downtime_active=False,
                    rail="upi_autopay",
                    segment_proxy=payer.segment,
                    notices_sent_this_cycle=notices_sent_this_cycle,
                    days_since_last_notice=None,
                )
            )
        )
    x = artifact.encoder.transform(rows)
    raw_probs = artifact.model.predict_proba(x)[:, 1]
    calibrated = artifact.isotonic.predict(raw_probs)
    return tuple(float(p) for p in calibrated)
