# ADR 0003 — Phase 4 dependencies: scikit-learn, matplotlib, numpy

- **scikit-learn** (runtime): `HistGradientBoostingClassifier` for the
  success model and `IsotonicRegression` for calibration are named
  explicitly in docs K.3 ("GBM + isotonic is correct" — LLMs are
  explicitly rejected there as badly-calibrated probability estimators).
  No lighter alternative in the stdlib does either.
- **numpy** (runtime): scikit-learn depends on it regardless; used
  directly here for feature-array construction rather than going through
  scikit-learn's API indirectly.
- **matplotlib** (runtime): the reliability diagram (`reports/
  calibration.png`) is an explicit, named deliverable (CLAUDE.md's Phase 4
  row) — there's no way to produce it without a plotting library, and
  matplotlib is the standard choice with the least ceremony for one static
  PNG.

Not added: **pandas**. Feature assembly is handled by a small hand-rolled
encoder (`backend/app/ml/features.py`) producing plain numpy arrays with
categorical columns integer-encoded and passed via
`HistGradientBoostingClassifier`'s native `categorical_features` support —
avoids a real dependency for what a ~40-line encoder covers.
