"""Versioned model artifacts (docs K.3: "feature hash + model version
stored" is part of every plan's audit trail — the `plans.model_version`
column already sitting in migrations/0001_core.sql becomes real here).

Artifacts are content-addressed: the version string is a hash of the
pickled bytes, so two training runs that happen to produce byte-identical
models get the same version, and any difference in inputs (corpus, model
hyperparameters, encoder) shows up as a different version automatically.
Not committed to the repo — regenerable via `make train`, same pattern as
data/generated/.
"""
from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

from app.ml.features import FeatureEncoder

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "ml" / "artifacts"


@dataclass(frozen=True)
class ModelArtifact:
    model: HistGradientBoostingClassifier
    encoder: FeatureEncoder
    isotonic: IsotonicRegression
    version: str


def save(
    model: HistGradientBoostingClassifier,
    encoder: FeatureEncoder,
    isotonic: IsotonicRegression,
    *,
    artifacts_dir: Path = ARTIFACTS_DIR,
) -> str:
    payload: dict[str, Any] = {"model": model, "encoder": encoder, "isotonic": isotonic}
    blob = pickle.dumps(payload)
    version = hashlib.sha256(blob).hexdigest()[:16]
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / f"model_{version}.pkl").write_bytes(blob)
    return version


def load(version: str, *, artifacts_dir: Path = ARTIFACTS_DIR) -> ModelArtifact:
    blob = (artifacts_dir / f"model_{version}.pkl").read_bytes()
    payload = pickle.loads(blob)  # noqa: S301 — our own artifact, not untrusted input
    return ModelArtifact(
        model=payload["model"], encoder=payload["encoder"], isotonic=payload["isotonic"],
        version=version,
    )
