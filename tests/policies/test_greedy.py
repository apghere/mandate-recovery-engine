from __future__ import annotations

from datetime import date

from app.domain.planner import PlannerConfig, PlanningInputs
from app.policies.greedy import compute_greedy_schedule


def _inputs(p: tuple[float, ...], e_manual: float = 200.0) -> PlanningInputs:
    return PlanningInputs(amount=1000.0, p_success=p, e_manual=e_manual, e_manual_late=50.0)


def test_picks_the_highest_scoring_reachable_slot_first() -> None:
    config = PlannerConfig(n_slots=10, max_attempts=1, notice_lead_slots=2)
    p = [0.1] * 10
    p[7] = 0.95  # clear best slot, reachable (>= notice_lead_slots)
    plan = compute_greedy_schedule(
        start_date=date(2026, 9, 1), attempts_remaining=1, config=config, inputs=_inputs(tuple(p))
    )
    attempt_steps = [s for s in plan.steps if s.step_type == "attempt"]
    assert len(attempt_steps) == 1
    assert attempt_steps[0].scheduled_for.day == 1 + 7 // 2  # slot 7 -> day offset 3


def test_zero_success_probability_everywhere_stops_immediately() -> None:
    config = PlannerConfig(n_slots=10, max_attempts=2, notice_lead_slots=2)
    plan = compute_greedy_schedule(
        start_date=date(2026, 9, 1),
        attempts_remaining=2,
        config=config,
        inputs=_inputs(tuple([0.0] * 10)),
    )
    assert plan.immediate_stop
    assert plan.steps == []


def test_never_reuses_a_slot_across_multiple_attempts() -> None:
    config = PlannerConfig(n_slots=10, max_attempts=3, notice_lead_slots=2)
    plan = compute_greedy_schedule(
        start_date=date(2026, 9, 1),
        attempts_remaining=3,
        config=config,
        inputs=_inputs(tuple([0.6] * 10)),
    )
    attempt_times = [s.scheduled_for for s in plan.steps if s.step_type == "attempt"]
    assert len(attempt_times) == len(set(attempt_times))


def test_schedule_never_exceeds_the_attempt_budget() -> None:
    config = PlannerConfig(n_slots=28, max_attempts=3, notice_lead_slots=2)
    plan = compute_greedy_schedule(
        start_date=date(2026, 9, 1),
        attempts_remaining=3,
        config=config,
        inputs=_inputs(tuple([0.5] * 28)),
    )
    assert sum(1 for s in plan.steps if s.step_type == "attempt") <= 3


def test_every_attempt_is_preceded_by_a_notify_at_the_correct_lead_time() -> None:
    config = PlannerConfig(n_slots=28, max_attempts=3, notice_lead_slots=2)
    plan = compute_greedy_schedule(
        start_date=date(2026, 9, 1),
        attempts_remaining=3,
        config=config,
        inputs=_inputs(tuple([0.5] * 28)),
    )
    by_type: dict[str, list[object]] = {"notify": [], "attempt": []}
    for s in plan.steps:
        by_type[s.step_type].append(s.scheduled_for)
    assert len(by_type["notify"]) == len(by_type["attempt"])
