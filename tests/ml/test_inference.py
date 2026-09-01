from __future__ import annotations

from datetime import date

from app.domain.types import Cause
from app.ml.calibrate import fit_isotonic
from app.ml.corpus import corpus_to_features_and_labels, generate_corpus
from app.ml.inference import PayerContext, payer_context_from_row, score_slots, slot_datetime
from app.ml.registry import ModelArtifact
from app.ml.train import fit_success_model

from data.generator import generate_population


def _small_artifact() -> ModelArtifact:
    rows = generate_corpus("calibration", samples_per_payer=1)
    features, labels = corpus_to_features_and_labels(rows)
    model, encoder = fit_success_model(features, labels)
    isotonic = fit_isotonic(model, encoder, features, labels)
    return ModelArtifact(model=model, encoder=encoder, isotonic=isotonic, version="test")


def test_slot_datetime_two_slots_per_day() -> None:
    start = date(2026, 9, 1)
    assert slot_datetime(start, 0).day == 1
    assert slot_datetime(start, 1).day == 1
    assert slot_datetime(start, 2).day == 2
    assert slot_datetime(start, 0).hour != slot_datetime(start, 1).hour


def test_payer_context_from_row_reads_expected_fields() -> None:
    row = {
        "id": "PAYER00001",
        "segment": "salaried",
        "credit_day": 5,
        "mean_balance": 20000.5,
        "balance_volatility": 0.6,
        "issuer_code": "ISS01",
        "chronic_fail_propensity": 0.1,
        "annoyance_sensitivity": 0.3,
        "mandate_amount": 500.0,
        "split": "train",
    }
    ctx = payer_context_from_row(row)
    assert ctx.payer_id == "PAYER00001"
    assert ctx.credit_day == 5
    assert ctx.mean_balance == 20000.5


def test_score_slots_returns_one_probability_per_slot_in_valid_range() -> None:
    artifact = _small_artifact()
    payer = generate_population(seed=1, n=1)[0]
    ctx = PayerContext(
        payer_id=payer.payer_id,
        segment=payer.segment,
        credit_day=payer.credit_day,
        mean_balance=payer.mean_balance,
        balance_volatility=payer.balance_volatility,
        issuer_code=payer.issuer_code,
        chronic_fail_propensity=payer.chronic_fail_propensity,
        mandate_amount=payer.mandate_amount,
    )
    probs = score_slots(
        artifact,
        payer=ctx,
        start_date=date(2026, 9, 1),
        n_slots=28,
        attempt_sequence_no=2,
        cause=Cause.INSUFFICIENT_FUNDS,
        consecutive_prior_failures=1,
    )
    assert len(probs) == 28
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_score_slots_is_deterministic() -> None:
    artifact = _small_artifact()
    payer = generate_population(seed=1, n=1)[0]
    ctx = PayerContext(
        payer_id=payer.payer_id,
        segment=payer.segment,
        credit_day=payer.credit_day,
        mean_balance=payer.mean_balance,
        balance_volatility=payer.balance_volatility,
        issuer_code=payer.issuer_code,
        chronic_fail_propensity=payer.chronic_fail_propensity,
        mandate_amount=payer.mandate_amount,
    )
    kwargs = dict(
        payer=ctx,
        start_date=date(2026, 9, 1),
        n_slots=10,
        attempt_sequence_no=2,
        cause=Cause.INSUFFICIENT_FUNDS,
        consecutive_prior_failures=0,
    )
    a = score_slots(artifact, **kwargs)  # type: ignore[arg-type]
    b = score_slots(artifact, **kwargs)  # type: ignore[arg-type]
    assert a == b
