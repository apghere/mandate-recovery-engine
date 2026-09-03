"""The reliability-diagram half of docs §K.3 -- split out of calibrate.py
(2026-09-03, free-tier deploy pass) so the live request-serving path
(app/policies/live.py -> app/ml/calibrate.py::fit_isotonic) never imports
matplotlib. Every actual caller of *this* module is offline tooling
(scripts/train.py, tests) -- nothing in the deployed Vercel function's
import chain reaches here, which matters concretely: Vercel Hobby caps
function duration at 10s with no way to raise it, and matplotlib's own
import cost was pure overhead on every cold start for a plot the live
path never draws.
"""
from __future__ import annotations

from collections.abc import Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss

from app.ml.calibrate import CalibrationReport, _binned_curve, expected_calibration_error
from app.ml.features import FeatureEncoder, FeatureRow


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


def _plot_reliability_diagram(
    y_true: np.ndarray, raw_probs: np.ndarray, calibrated_probs: np.ndarray, out_path: str
) -> None:
    # Sparse bins (few samples) are visually identical to well-populated
    # ones unless labelled -- and a sparse bin at the extreme end of the
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
