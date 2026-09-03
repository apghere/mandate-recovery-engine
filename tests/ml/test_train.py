from __future__ import annotations

from app.ml.corpus import corpus_to_features_and_labels, generate_corpus
from app.ml.train import fit_success_model


def test_fit_success_model_predicts_probabilities_in_valid_range() -> None:
    rows = generate_corpus("calibration", samples_per_payer=1)
    features, labels = corpus_to_features_and_labels(rows)
    model, encoder = fit_success_model(features, labels)

    x = encoder.transform(features)
    probs = model.predict_proba(x)[:, 1]
    assert (probs >= 0.0).all()
    assert (probs <= 1.0).all()
    assert len(probs) == len(rows)


def test_fit_success_model_is_deterministic() -> None:
    rows = generate_corpus("calibration", samples_per_payer=1)
    features, labels = corpus_to_features_and_labels(rows)

    model_a, encoder_a = fit_success_model(features, labels)
    model_b, encoder_b = fit_success_model(features, labels)

    probs_a = model_a.predict_proba(encoder_a.transform(features))[:, 1]
    probs_b = model_b.predict_proba(encoder_b.transform(features))[:, 1]
    assert (probs_a == probs_b).all()


def test_model_does_better_than_chance_on_its_own_training_distribution() -> None:
    """Not a rigorous holdout check (that's test_corpus's/train script's
    job) — just a sanity floor: the model should separate success from
    failure at all, given the deliberately non-trivial signal J.2's
    timing model injects."""
    rows = generate_corpus("train", samples_per_payer=2)
    features, labels = corpus_to_features_and_labels(rows)
    model, encoder = fit_success_model(features, labels)
    x = encoder.transform(features)
    probs = model.predict_proba(x)[:, 1]

    successes = [p for p, y in zip(probs, labels, strict=True) if y == 1]
    failures = [p for p, y in zip(probs, labels, strict=True) if y == 0]
    assert sum(successes) / len(successes) > sum(failures) / len(failures)
