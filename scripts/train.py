"""Phase 4: fit the success model, calibrate it, emit the reliability
diagram (CLAUDE.md's Phase 4 definition of done).

`train` -> fit GBM. `calibration` -> fit isotonic (never reused elsewhere,
docs §J.5). `dev` -> genuinely held-out Brier/ECE before/after. `test` is
never touched here or anywhere before Day 5 (docs §J.5 / §T red-team
point 3).

Usage: `make train`.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.ml.calibrate import evaluate_and_plot, fit_isotonic
from app.ml.corpus import corpus_to_features_and_labels, generate_corpus
from app.ml.registry import save
from app.ml.train import fit_success_model

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
CALIBRATION_PNG = REPORTS_DIR / "calibration.png"
METRICS_JSON = REPORTS_DIR / "calibration_metrics.json"


def main() -> None:
    print("generating training corpus (train split)...")
    train_rows = generate_corpus("train")
    train_features, train_labels = corpus_to_features_and_labels(train_rows)
    print(f"  {len(train_rows)} rows, {sum(train_labels) / len(train_labels):.3f} success rate")

    print("fitting HistGradientBoostingClassifier...")
    model, encoder = fit_success_model(train_features, train_labels)

    print("generating calibration corpus (calibration split)...")
    calib_rows = generate_corpus("calibration")
    calib_features, calib_labels = corpus_to_features_and_labels(calib_rows)
    print(f"  {len(calib_rows)} rows")

    print("fitting isotonic calibration...")
    isotonic = fit_isotonic(model, encoder, calib_features, calib_labels)

    print("generating held-out evaluation corpus (dev split)...")
    dev_rows = generate_corpus("dev")
    dev_features, dev_labels = corpus_to_features_and_labels(dev_rows)
    print(f"  {len(dev_rows)} rows")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"evaluating + writing reliability diagram to {CALIBRATION_PNG}...")
    report = evaluate_and_plot(
        model, encoder, isotonic, dev_features, dev_labels, str(CALIBRATION_PNG)
    )

    version = save(model, encoder, isotonic)

    METRICS_JSON.write_text(
        json.dumps(
            {
                "model_version": version,
                "train_rows": len(train_rows),
                "calibration_rows": len(calib_rows),
                "eval_rows": len(dev_rows),
                "eval_split": "dev",
                "brier_before": report.brier_before,
                "brier_after": report.brier_after,
                "ece_before": report.ece_before,
                "ece_after": report.ece_after,
                "bin_counts_before": report.bin_counts_before,
                "bin_counts_after": report.bin_counts_after,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print(f"model version: {version}")
    print(f"  Brier score:  {report.brier_before:.4f} -> {report.brier_after:.4f}")
    print(f"  ECE:          {report.ece_before:.4f} -> {report.ece_after:.4f}")
    if report.ece_after > report.ece_before:
        print("  NOTE: calibration made ECE worse on this run -- report honestly, don't hide it.")


if __name__ == "__main__":
    main()
