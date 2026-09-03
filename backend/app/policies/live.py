"""The real, live policy selection used by the production `/events`
endpoint (api/app.py).

This is NOT scripts/replay_compare.py's or evaluation/runner.py's
compute_plan closures — those are benchmark-only tooling that deliberately
hold `cause` fixed at INSUFFICIENT_FUNDS for a fair aggregate timing
comparison across policies regardless of what actually declined (see
their own docstrings for why that's the right call for a benchmark). This
module is the one place a live event's *actually normalized* cause for
*this specific case*, and a mandate's *actually persisted* payer row,
both reach the planner's scoring step — the thing that makes docs W2
("cause = MANDATE_REVOKED -> planner values every continuation at ~zero
-> stopping rule fires -> zero attempts consumed") a real, live workflow
and not only something exercised by a hand-constructed p_success array in
a test.

Model artifact: lazily trained once (in-process, ~2s per `make train`'s
own report) and cached for the life of the process. Loading a real,
content-addressed, registered artifact from app.ml.registry instead of
training fresh on first use is real future work — the registry module and
`make train` already exist for exactly that, this just doesn't consume
them yet.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

from app import repo
from app.db import Conn
from app.domain.planner import PlannerConfig, PlanningInputs
from app.domain.types import Cause
from app.ingest import PlanChoice
from app.ml.calibrate import fit_isotonic
from app.ml.corpus import corpus_to_features_and_labels, generate_corpus
from app.ml.inference import payer_context_from_row, score_slots
from app.ml.registry import ModelArtifact
from app.ml.train import fit_success_model
from app.policies.fixed import POLICY_VERSION as FIXED_POLICY_VERSION
from app.policies.fixed import compute_fixed_schedule
from app.policies.mre import POLICY_VERSION as MRE_POLICY_VERSION
from app.policies.mre import compute_mre_schedule

N_SLOTS = 28
E_MANUAL = 150.0
E_MANUAL_LATE = 30.0
ATTEMPT_SEQUENCE_NO_FOR_SCORING = 2  # first MRE-scheduled attempt is always seq 2

_artifact: ModelArtifact | None = None


def get_artifact() -> ModelArtifact:
    """Train once, cache for the process's lifetime. Never touches dev or
    test — train + calibration splits only, same discipline as
    scripts/train.py and evaluation/runner.py."""
    global _artifact
    if _artifact is not None:
        return _artifact
    train_rows = generate_corpus("train")
    train_features, train_labels = corpus_to_features_and_labels(train_rows)
    model, encoder = fit_success_model(train_features, train_labels)
    calib_rows = generate_corpus("calibration")
    calib_features, calib_labels = corpus_to_features_and_labels(calib_rows)
    isotonic = fit_isotonic(model, encoder, calib_features, calib_labels)
    _artifact = ModelArtifact(model=model, encoder=encoder, isotonic=isotonic, version="live-v1")
    return _artifact


def _fixed_fallback(due_date: date, _cause: Cause) -> PlanChoice:
    return PlanChoice(policy_version=FIXED_POLICY_VERSION, steps=compute_fixed_schedule(due_date))


def select_compute_plan(
    conn: Conn, mandate: dict[str, object]
) -> Callable[[date, Cause], PlanChoice]:
    """Builds the real, cause-aware compute_plan callback for this
    mandate's payer. Falls back to the P0 fixed baseline — gracefully,
    not a crash — when there's no payers row for this mandate yet (Phase
    5 simplification: payer enrichment doesn't cover every mandate)."""
    payer_id = mandate["payer_id"]
    assert isinstance(payer_id, str)
    payer_row = repo.get_payer(conn, payer_id)
    if payer_row is None:
        return _fixed_fallback

    artifact = get_artifact()
    payer_ctx = payer_context_from_row(payer_row)

    def compute_plan(due_date: date, cause: Cause) -> PlanChoice:
        probs = score_slots(
            artifact,
            payer=payer_ctx,
            start_date=due_date,
            n_slots=N_SLOTS,
            attempt_sequence_no=ATTEMPT_SEQUENCE_NO_FOR_SCORING,
            cause=cause,
            consecutive_prior_failures=0,
        )
        config = PlannerConfig(n_slots=N_SLOTS, max_attempts=4)
        inputs = PlanningInputs(
            amount=payer_ctx.mandate_amount,
            p_success=probs,
            e_manual=E_MANUAL,
            e_manual_late=E_MANUAL_LATE,
        )
        plan = compute_mre_schedule(
            start_date=due_date, attempts_remaining=3, config=config, inputs=inputs
        )
        return PlanChoice(
            policy_version=MRE_POLICY_VERSION,
            steps=plan.steps,
            immediate_stop=plan.immediate_stop,
            expected_value=plan.expected_value,
            solver_ms=plan.solver_ms,
        )

    return compute_plan
