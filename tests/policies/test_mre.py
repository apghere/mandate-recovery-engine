from __future__ import annotations

from datetime import date

from app.domain.planner import PlannerConfig, PlanningInputs
from app.policies.mre import compute_mre_schedule


def _inputs(n_slots: int, p: tuple[float, ...], e_manual: float = 200.0) -> PlanningInputs:
    return PlanningInputs(
        amount=1000.0, p_success=p, e_manual=e_manual, e_manual_late=50.0
    )


def test_favorable_slots_produce_a_non_empty_schedule() -> None:
    config = PlannerConfig(n_slots=10, max_attempts=3)
    inputs = _inputs(10, tuple([0.9] * 10))
    plan = compute_mre_schedule(
        start_date=date(2026, 9, 1), attempts_remaining=3, config=config, inputs=inputs
    )
    assert not plan.immediate_stop
    assert any(s.step_type == "attempt" for s in plan.steps)
    assert any(s.step_type == "notify" for s in plan.steps)


def test_zero_success_probability_everywhere_stops_immediately() -> None:
    """The docs §W2 scenario: cause=MANDATE_REVOKED-style, no continuation
    is worth anything -- zero attempts consumed, immediate escalation."""
    config = PlannerConfig(n_slots=10, max_attempts=3)
    inputs = _inputs(10, tuple([0.0] * 10))
    plan = compute_mre_schedule(
        start_date=date(2026, 9, 1), attempts_remaining=3, config=config, inputs=inputs
    )
    assert plan.immediate_stop
    assert plan.steps == []


def test_zero_attempts_remaining_never_schedules_an_attempt() -> None:
    config = PlannerConfig(n_slots=10, max_attempts=3)
    inputs = _inputs(10, tuple([0.9] * 10))
    plan = compute_mre_schedule(
        start_date=date(2026, 9, 1), attempts_remaining=0, config=config, inputs=inputs
    )
    assert not any(s.step_type == "attempt" for s in plan.steps)


def test_schedule_never_exceeds_the_attempt_budget() -> None:
    config = PlannerConfig(n_slots=28, max_attempts=3)
    inputs = _inputs(28, tuple([0.5] * 28))
    plan = compute_mre_schedule(
        start_date=date(2026, 9, 1), attempts_remaining=3, config=config, inputs=inputs
    )
    attempt_count = sum(1 for s in plan.steps if s.step_type == "attempt")
    assert attempt_count <= 3


def test_every_attempt_is_preceded_by_a_notify() -> None:
    config = PlannerConfig(n_slots=28, max_attempts=3, notice_lead_slots=2)
    inputs = _inputs(28, tuple([0.5] * 28))
    plan = compute_mre_schedule(
        start_date=date(2026, 9, 1), attempts_remaining=3, config=config, inputs=inputs
    )
    notify_times = {s.scheduled_for for s in plan.steps if s.step_type == "notify"}
    for step in plan.steps:
        if step.step_type != "attempt":
            continue
        assert any(nt < step.scheduled_for for nt in notify_times)


def test_solver_ms_is_recorded_and_non_negative() -> None:
    config = PlannerConfig(n_slots=10, max_attempts=3)
    inputs = _inputs(10, tuple([0.5] * 10))
    plan = compute_mre_schedule(
        start_date=date(2026, 9, 1), attempts_remaining=3, config=config, inputs=inputs
    )
    assert plan.solver_ms >= 0.0
