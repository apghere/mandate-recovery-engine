from __future__ import annotations

from app.domain.types import Cause, MandateSnapshot, RetryDisposition
from app.ml.features import (
    CATEGORICAL_FEATURES,
    FEATURE_NAMES,
    FeatureEncoder,
    assemble_features,
)


def _snapshot(**overrides: object) -> MandateSnapshot:
    defaults: dict[str, object] = {
        "cause": Cause.INSUFFICIENT_FUNDS,
        "disposition": RetryDisposition.RETRY_TIMING_SENSITIVE,
        "attempt_sequence_no": 2,
        "hours_since_last_failure": 48.0,
        "day_of_month": 15,
        "days_to_credit_day": 5,
        "slot_of_day": 0,
        "day_of_week": 2,
        "amount": 500,
        "amount_over_historical_mean": 1.0,
        "mandate_age_days": 90,
        "payer_prior_success_rate": 0.8,
        "consecutive_prior_failures": 1,
        "issuer_historical_success_rate": 0.9,
        "issuer_downtime_active": False,
        "rail": "upi_autopay",
        "segment_proxy": "salaried",
        "notices_sent_this_cycle": 1,
        "days_since_last_notice": 1.5,
    }
    defaults.update(overrides)
    return MandateSnapshot(**defaults)  # type: ignore[arg-type]


def test_assemble_features_covers_every_declared_feature_name() -> None:
    row = assemble_features(_snapshot())
    assert set(row) == set(FEATURE_NAMES)


def test_assemble_features_is_deterministic() -> None:
    s = _snapshot()
    assert assemble_features(s) == assemble_features(s)


def test_none_days_since_last_notice_becomes_a_sentinel() -> None:
    row = assemble_features(_snapshot(days_since_last_notice=None))
    assert row["days_since_last_notice"] == -1.0


def test_encoder_fit_transform_shapes_and_categorical_columns() -> None:
    rows = [
        assemble_features(_snapshot(cause=Cause.INSUFFICIENT_FUNDS)),
        assemble_features(_snapshot(cause=Cause.TIMEOUT)),
    ]
    encoder = FeatureEncoder.fit(rows)
    arr = encoder.transform(rows)
    assert arr.shape == (2, len(FEATURE_NAMES))
    for name in CATEGORICAL_FEATURES:
        assert name in encoder.category_maps


def test_encoder_transform_is_reproducible_and_reused_not_refit() -> None:
    train_rows = [
        assemble_features(_snapshot(cause=Cause.INSUFFICIENT_FUNDS)),
        assemble_features(_snapshot(cause=Cause.TIMEOUT)),
    ]
    encoder = FeatureEncoder.fit(train_rows)
    a = encoder.transform(train_rows)
    b = encoder.transform(train_rows)
    assert (a == b).all()


def test_encoder_handles_an_unseen_category_at_inference_time() -> None:
    train_rows = [assemble_features(_snapshot(cause=Cause.INSUFFICIENT_FUNDS))]
    encoder = FeatureEncoder.fit(train_rows)
    unseen_row = assemble_features(_snapshot(cause=Cause.RISK_DECLINE))
    arr = encoder.transform([unseen_row])
    cause_idx = FEATURE_NAMES.index("cause")
    assert arr[0, cause_idx] == len(encoder.category_maps["cause"])
