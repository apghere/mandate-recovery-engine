from __future__ import annotations

from pathlib import Path

from app.ml import registry
from app.ml.calibrate import fit_isotonic
from app.ml.corpus import corpus_to_features_and_labels, generate_corpus
from app.ml.train import fit_success_model


def _fit_small_artifact() -> tuple[object, object, object, list[dict[str, str | float]]]:
    rows = generate_corpus("calibration", samples_per_payer=1)
    features, labels = corpus_to_features_and_labels(rows)
    model, encoder = fit_success_model(features, labels)
    isotonic = fit_isotonic(model, encoder, features, labels)
    return model, encoder, isotonic, features


def test_save_and_load_round_trips_identical_predictions(tmp_path: Path) -> None:
    model, encoder, isotonic, features = _fit_small_artifact()
    version = registry.save(model, encoder, isotonic, artifacts_dir=tmp_path)  # type: ignore[arg-type]

    loaded = registry.load(version, artifacts_dir=tmp_path)
    assert loaded.version == version

    x = encoder.transform(features)  # type: ignore[attr-defined]
    original_probs = model.predict_proba(x)[:, 1]  # type: ignore[attr-defined]
    loaded_probs = loaded.model.predict_proba(loaded.encoder.transform(features))[:, 1]
    assert (original_probs == loaded_probs).all()


def test_save_is_deterministic_given_identical_inputs(tmp_path: Path) -> None:
    model, encoder, isotonic, _features = _fit_small_artifact()
    v1 = registry.save(model, encoder, isotonic, artifacts_dir=tmp_path)  # type: ignore[arg-type]
    v2 = registry.save(model, encoder, isotonic, artifacts_dir=tmp_path)  # type: ignore[arg-type]
    assert v1 == v2


def test_different_models_get_different_versions(tmp_path: Path) -> None:
    model_a, encoder_a, isotonic_a, _ = _fit_small_artifact()
    rows_b = generate_corpus("dev", samples_per_payer=1)
    features_b, labels_b = corpus_to_features_and_labels(rows_b)
    model_b, encoder_b = fit_success_model(features_b, labels_b)
    isotonic_b = fit_isotonic(model_b, encoder_b, features_b, labels_b)

    v_a = registry.save(model_a, encoder_a, isotonic_a, artifacts_dir=tmp_path)  # type: ignore[arg-type]
    v_b = registry.save(model_b, encoder_b, isotonic_b, artifacts_dir=tmp_path)  # type: ignore[arg-type]
    assert v_a != v_b
