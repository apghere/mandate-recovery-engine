"""Isotonic calibration + the reliability diagram (docs §K.3, §N Day 3).

Isotonic is fit on the `calibration` split, never reused elsewhere per
docs §J.5. Brier score and ECE are then reported on a genuinely held-out
split (`dev`) — evaluating on the same data isotonic was fit on would
trivially look well-calibrated regardless of whether the underlying model
is any good. `test` is never touched here (see docs/RAZORPAY_TESTMODE_
FINDINGS.md and §T's red-team point 3 for why that discipline matters).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss

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


def evaluate_and_plot(
    model: HistGradientBoostingClassifier,
    encoder: FeatureEncoder,
    isotonic: IsotonicRegression,
    eval_features: list[FeatureRow],
    eval_labels: Sequence[int],
    out_path: str,
) -> CalibrationReport:
    x = encoder.transform(eval_features)
    y = np.array(eval_labels, dtype=np.float64)
    raw_probs = model.predict_proba(x)[:, 1]
    calibrated_probs = isotonic.predict(raw_probs)

    brier_before = float(brier_score_loss(y, raw_probs))
    brier_after = float(brier_score_loss(y, calibrated_probs))
    ece_before = expected_calibration_error(y, raw_probs)
    ece_after = expected_calibration_error(y, calibrated_probs)

    _plot_reliability_diagram(y, raw_probs, calibrated_probs, out_path)
    _, _, counts_before = _binned_curve(y, raw_probs)
    _, _, counts_after = _binned_curve(y, calibrated_probs)

    return CalibrationReport(
        brier_before=brier_before,
        brier_after=brier_after,
        ece_before=ece_before,
        ece_after=ece_after,
        bin_counts_before=counts_before,
        bin_counts_after=counts_after,
    )


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


def _plot_reliability_diagram(
    y_true: np.ndarray, raw_probs: np.ndarray, calibrated_probs: np.ndarray, out_path: str
) -> None:
    # Sparse bins (few samples) are visually identical to well-populated
    # ones unless labelled — and a sparse bin at the extreme end of the
    # probability range is exactly what a red-team reviewer will poke at
    # first (docs §T). Annotate every point with its n so it's never a
    # hidden gotcha.
    fig, axes = plt.subplots(1, 2, figsize=(10, 5), sharey=True)
    for ax, probs, title in (
        (axes[0], raw_probs, "Before calibration (raw GBM)"),
        (axes[1], calibrated_probs, "After isotonic calibration"),
    ):
        conf, acc, counts = _binned_curve(y_true, probs)
        ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="perfect calibration")
        ax.plot(conf, acc, marker="o", label="observed")
        for c, a, n in zip(conf, acc, counts, strict=True):
            ax.annotate(
                f"n={n}", (c, a), textcoords="offset points", xytext=(4, 6), fontsize=7,
                color="dimgray",
            )
        ax.set_xlabel("predicted probability")
        ax.set_title(title)
        ax.legend(loc="upper left", fontsize=8)
    axes[0].set_ylabel("observed success rate")
    fig.suptitle("MRE success-model calibration (docs §K.3)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
