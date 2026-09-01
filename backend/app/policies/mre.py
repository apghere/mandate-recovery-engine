"""MRE — the constrained-optimal-stopping policy (docs §K.4).

Solves the DP once for the cycle's current state and extracts the
deterministic "assume every attempt fails" walk through the optimal
policy table as a pre-committed schedule — the same (step_type,
scheduled_for) shape app/policies/fixed.py produces, so it plugs into the
existing worker/outbox infrastructure (Phase 3) without needing a
dynamically-re-planning worker. If an attempt actually succeeds in
reality, the existing cancel_pending_steps_for_cycle machinery (already
used by P0) cancels whatever was left — which is exactly correct here
too, since "assume failure" was only ever a scheduling convenience, not a
claim about what will happen.

This is a real, documented scope boundary, not an oversight: a fully
adaptive planner that re-solves after every real-world signal (issuer
downtime, mid-plan revocation, a notice-send failure) is Phase 7+
territory (that re-solve is literally docs §W3's "notice failure -> debit
denied -> plan re-solves around the shortened horizon" scenario — the
policy engine's independent re-check at execution time is what makes
*not* re-planning here safe rather than reckless). What's captured here
is the actual thesis: timing-optimized slot selection and the stopping
rule.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import date

from app.domain.planner import (
    NO_NOTICE,
    PlannerConfig,
    PlanningInputs,
    advance_notice,
    solve,
)
from app.domain.types import ActionType
from app.ml.inference import slot_datetime
from app.policies.fixed import ScheduledStep

POLICY_VERSION = "MRE-dp-v1"


@dataclass(frozen=True)
class MrePlan:
    steps: list[ScheduledStep]
    immediate_stop: bool  # True iff the root action is STOP_AND_ESCALATE
    expected_value: float
    solver_ms: float


def compute_mre_schedule(
    *,
    start_date: date,
    attempts_remaining: int,
    config: PlannerConfig,
    inputs: PlanningInputs,
) -> MrePlan:
    t0 = _time.perf_counter()
    result = solve(config, inputs)
    solver_ms = (_time.perf_counter() - t0) * 1000

    root_action = result.root_action(attempts_remaining)
    if root_action == ActionType.STOP_AND_ESCALATE:
        return MrePlan(
            steps=[],
            immediate_stop=True,
            expected_value=result.root_value(attempts_remaining),
            solver_ms=solver_ms,
        )

    steps: list[ScheduledStep] = []
    pending_notify_index: int | None = None  # index in `steps` awaiting its covers_debit_at
    state = (0, attempts_remaining, NO_NOTICE)
    while state[0] < config.n_slots:
        t, b, n = state
        action = result.actions[state]
        if action == ActionType.STOP_AND_ESCALATE:
            break
        if action == ActionType.IDLE:
            state = (t + 1, b, advance_notice(n, sent_now=False, config=config))
        elif action == ActionType.NOTIFY:
            steps.append(ScheduledStep("notify", slot_datetime(start_date, t)))
            pending_notify_index = len(steps) - 1
            state = (t + 1, b, advance_notice(n, sent_now=True, config=config))
        elif action == ActionType.ATTEMPT:
            attempt_at = slot_datetime(start_date, t)
            steps.append(ScheduledStep("attempt", attempt_at))
            if pending_notify_index is not None:
                notify_step = steps[pending_notify_index]
                steps[pending_notify_index] = ScheduledStep(
                    notify_step.step_type, notify_step.scheduled_for, covers_debit_at=attempt_at
                )
                pending_notify_index = None
            # Deterministic "assume it fails" walk — see module docstring.
            state = (t + 1, b - 1, NO_NOTICE)
        else:  # pragma: no cover - exhaustive over ActionType
            raise AssertionError(f"unhandled action {action}")

    return MrePlan(
        steps=steps,
        immediate_stop=False,
        expected_value=result.root_value(attempts_remaining),
        solver_ms=solver_ms,
    )
