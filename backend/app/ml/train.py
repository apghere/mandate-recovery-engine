"""Fit the success model (docs K.3): HistGradientBoostingClassifier over
the pure feature-assembly pipeline. Not an LLM — K.1 rejects LLMs here
explicitly as badly-calibrated probability estimators; a planner consumes
expectations, not vibes.
"""
from __future__ import annotations

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from app.ml.features import CATEGORICAL_INDICES, FeatureEncoder, FeatureRow

RANDOM_STATE = 20260901


def fit_success_model(
    train_features: list[FeatureRow], train_labels: list[int]
) -> tuple[HistGradientBoostingClassifier, FeatureEncoder]:
    encoder = FeatureEncoder.fit(train_features)
    x = encoder.transform(train_features)
    y = np.array(train_labels, dtype=np.int64)
    model = HistGradientBoostingClassifier(
        categorical_features=list(CATEGORICAL_INDICES),
        random_state=RANDOM_STATE,
    )
    model.fit(x, y)
    return model, encoder
