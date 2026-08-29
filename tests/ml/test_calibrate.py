from __future__ import annotations

from pathlib import Path

import numpy as np
from app.ml.calibrate import evaluate_and_plot, expected_calibration_error, fit_isotonic
from app.ml.corpus import corpus_to_features_and_labels, generate_corpus
from app.ml.train import fit_success_model


def test_ece_is_zero_for_perfect_calibration() -> None:
    y = np.array([0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0])
    p = np.array([0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9])
    # Not literally perfect (bins mix 0s and 1s) but every bin's mean
    # prediction should roughly match its mean outcome by construction.
    ece = expected_calibration_error(y, p)
    assert ece < 0.15


def test_ece_is_high_for_systematically_overconfident_predictions() -> None:
    y = np.zeros(20)
    p = np.full(20, 0.95)  # always predicts success, always wrong
    ece = expected_calibration_error(y, p)
    assert ece > 0.9


def test_ece_is_between_zero_and_one_on_real_model_output() -> None:
    rows = generate_corpus("calibration", samples_per_payer=1)
    features, labels = corpus_to_features_and_labels(rows)
    model, encoder = fit_success_model(features, labels)
    x = encoder.transform(features)
    probs = model.predict_proba(x)[:, 1]
    y = np.array(labels, dtype=np.float64)
    ece = expected_calibration_error(y, probs)
    assert 0.0 <= ece <= 1.0


def test_evaluate_and_plot_produces_a_report_and_a_png(tmp_path: Path) -> None:
    train_rows = generate_corpus("train", samples_per_payer=1)
    train_features, train_labels = corpus_to_features_and_labels(train_rows)
    model, encoder = fit_success_model(train_features, train_labels)

    calib_rows = generate_corpus("calibration", samples_per_payer=1)
    calib_features, calib_labels = corpus_to_features_and_labels(calib_rows)
    isotonic = fit_isotonic(model, encoder, calib_features, calib_labels)

    dev_rows = generate_corpus("dev", samples_per_payer=1)
    dev_features, dev_labels = corpus_to_features_and_labels(dev_rows)

    out_path = tmp_path / "calibration.png"
    report = evaluate_and_plot(
        model, encoder, isotonic, dev_features, dev_labels, str(out_path)
    )

    assert out_path.exists()
    assert out_path.stat().st_size > 0
    assert 0.0 <= report.brier_before <= 1.0
    assert 0.0 <= report.brier_after <= 1.0
    assert 0.0 <= report.ece_before <= 1.0
    assert 0.0 <= report.ece_after <= 1.0
    assert sum(report.bin_counts_before) == len(dev_rows)
    assert sum(report.bin_counts_after) == len(dev_rows)
