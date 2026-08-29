"""Feature assembly: a pure function over MandateSnapshot (docs §K.3).

Used identically at training and inference time — the standard defence
against train/serve skew, worth saying out loud per the docs. This module
does no I/O and touches no clock; it's as close to `domain/`-pure as
something feeding a scikit-learn model can be.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.domain.types import MandateSnapshot

FeatureRow = dict[str, str | float]

FEATURE_NAMES: tuple[str, ...] = (
    "cause",
    "disposition",
    "attempt_sequence_no",
    "hours_since_last_failure",
    "day_of_month",
    "days_to_credit_day",
    "slot_of_day",
    "day_of_week",
    "amount",
    "amount_over_historical_mean",
    "mandate_age_days",
    "payer_prior_success_rate",
    "consecutive_prior_failures",
    "issuer_historical_success_rate",
    "issuer_downtime_active",
    "rail",
    "segment_proxy",
    "notices_sent_this_cycle",
    "days_since_last_notice",
)

CATEGORICAL_FEATURES: frozenset[str] = frozenset({"cause", "disposition", "rail", "segment_proxy"})
CATEGORICAL_INDICES: tuple[int, ...] = tuple(
    i for i, name in enumerate(FEATURE_NAMES) if name in CATEGORICAL_FEATURES
)

# days_since_last_notice is None before any notice has been sent this
# cycle. -1.0 is out of the valid (>=0) range, so a tree-based model can
# split on it cleanly as its own "no notice yet" branch.
_NO_NOTICE_SENTINEL = -1.0


def assemble_features(snapshot: MandateSnapshot) -> FeatureRow:
    return {
        "cause": snapshot.cause.value,
        "disposition": snapshot.disposition.value,
        "attempt_sequence_no": float(snapshot.attempt_sequence_no),
        "hours_since_last_failure": float(snapshot.hours_since_last_failure),
        "day_of_month": float(snapshot.day_of_month),
        "days_to_credit_day": float(snapshot.days_to_credit_day),
        "slot_of_day": float(snapshot.slot_of_day),
        "day_of_week": float(snapshot.day_of_week),
        "amount": float(snapshot.amount),
        "amount_over_historical_mean": float(snapshot.amount_over_historical_mean),
        "mandate_age_days": float(snapshot.mandate_age_days),
        "payer_prior_success_rate": float(snapshot.payer_prior_success_rate),
        "consecutive_prior_failures": float(snapshot.consecutive_prior_failures),
        "issuer_historical_success_rate": float(snapshot.issuer_historical_success_rate),
        "issuer_downtime_active": 1.0 if snapshot.issuer_downtime_active else 0.0,
        "rail": snapshot.rail,
        "segment_proxy": snapshot.segment_proxy,
        "notices_sent_this_cycle": float(snapshot.notices_sent_this_cycle),
        "days_since_last_notice": (
            float(snapshot.days_since_last_notice)
            if snapshot.days_since_last_notice is not None
            else _NO_NOTICE_SENTINEL
        ),
    }


@dataclass
class FeatureEncoder:
    """Fits category -> integer-code maps on training data. Must be fit
    exactly once and reused (never refit) at inference time — it is part
    of the model artifact, not a preprocessing convenience."""

    category_maps: dict[str, dict[str, int]] = field(default_factory=dict)

    @staticmethod
    def fit(rows: list[FeatureRow]) -> FeatureEncoder:
        category_maps: dict[str, dict[str, int]] = {}
        for name in FEATURE_NAMES:
            if name not in CATEGORICAL_FEATURES:
                continue
            values = sorted({str(row[name]) for row in rows})
            category_maps[name] = {v: i for i, v in enumerate(values)}
        return FeatureEncoder(category_maps=category_maps)

    def transform(self, rows: list[FeatureRow]) -> np.ndarray:
        arr = np.zeros((len(rows), len(FEATURE_NAMES)), dtype=np.float64)
        for i, row in enumerate(rows):
            for j, name in enumerate(FEATURE_NAMES):
                value = row[name]
                if name in CATEGORICAL_FEATURES:
                    mapping = self.category_maps[name]
                    key = str(value)
                    code = mapping.get(key, len(mapping))  # unseen -> new code
                    arr[i, j] = float(code)
                else:
                    arr[i, j] = float(value)
        return arr
