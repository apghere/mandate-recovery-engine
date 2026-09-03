"""Isotonic calibration (docs K.3, N Day 3) — the fitting half only.
The reliability-diagram plotting half lives in calibration_plot.py, split
out on 2026-09-03 specifically so this module, which the live request-
serving path imports for `fit_isotonic`, never pulls in matplotlib —
see that module's docstring for why that split matters for a Vercel
Hobby deploy.

Isotonic is fit on the `calibration` split, never reused elsewhere per
docs J.5. Brier score and ECE are then reported on a genuinely held-out
split (`dev`) — evaluating on the same data isotonic was fit on would
trivially look well-calibrated regardless of whether the underlying model
is any good. `test` is never touched here (see docs/RAZORPAY_TESTMODE_
FINDINGS.md and T's red-team point 3 for why that discipline matters).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

from app.ml.features import FeatureEncoder, FeatureRow

N_BINS = 10


def expected_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = N_BINS
) -> float:
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    n = len(y_true)
    for lo, hi in zip(bin_edges[:-1], bin_edges[1:], strict=True):
        mask = (y_prob >= lo) & (y_prob < hi if hi < 1.0 else y_prob <= hi)
        if not mask.any():
            continue
        bin_confidence = y_prob[mask].mean()
        bin_accuracy = y_true[mask].mean()
        ece += (mask.sum() / n) * abs(bin_accuracy - bin_confidence)
    return float(ece)


@dataclass(frozen=True)
class CalibrationReport:
    brier_before: float
    brier_after: float
    ece_before: float
    ece_after: float
    bin_counts_before: list[int]
    bin_counts_after: list[int]


def fit_isotonic(
    model: HistGradientBoostingClassifier,
    encoder: FeatureEncoder,
    calib_features: list[FeatureRow],
    calib_labels: Sequence[int],
) -> IsotonicRegression:
    x = encoder.transform(calib_features)
    raw_probs = model.predict_proba(x)[:, 1]
    y = np.array(calib_labels, dtype=np.float64)
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw_probs, y)
    return iso


def _binned_curve(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = N_BINS
) -> tuple[list[float], list[float], list[int]]:
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    confidences: list[float] = []
    accuracies: list[float] = []
    counts: list[int] = []
    for lo, hi in zip(bin_edges[:-1], bin_edges[1:], strict=True):
        mask = (y_prob >= lo) & (y_prob < hi if hi < 1.0 else y_prob <= hi)
        if not mask.any():
            continue
        confidences.append(float(y_prob[mask].mean()))
        accuracies.append(float(y_true[mask].mean()))
        counts.append(int(mask.sum()))
    return confidences, accuracies, counts
