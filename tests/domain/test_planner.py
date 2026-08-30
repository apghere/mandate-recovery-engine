from __future__ import annotations

import random

import pytest
from app.domain.planner import (
    NO_NOTICE,
    NOTICE_READY,
    PlannerConfig,
    PlanningInputs,
    State,
    advance_notice,
    solve,
)
from app.domain.types import ActionType
from hypothesis import given
from hypothesis import strategies as st


def _brute_force_value(
    config: PlannerConfig, inputs: PlanningInputs, state: State
) -> float:
    """Independently-coded top-down recursion over the exact same
    decision problem `solve()` computes bottom-up — no shared code path
    with solve()'s value/action construction, no memoization, so for a
    small horizon this really is "try every legal action sequence" rather
    than a disguised reimplementation of the same algorithm."""
    t, b, n = state
    if t == config.n_slots:
        return inputs.e_manual_late

    success_value = inputs.amount + inputs.continuation_value
    p = inputs.p_success[t]
    idle_next = (t + 1, b, advance_notice(n, sent_now=False, config=config))
    candidates = [_brute_force_value(config, inputs, idle_next)]

    if n == NO_NOTICE:
        notify_next = (t + 1, b, advance_notice(n, sent_now=True, config=config))
        candidates.append(
            -config.notify_cost
            - config.optout_hazard_cost
            + _brute_force_value(config, inputs, notify_next)
        )

    if b > 0 and n == NOTICE_READY:
        fail_next = (t + 1, b - 1, NO_NOTICE)
        candidates.append(
            p * success_value
            + (1 - p) * (-config.attempt_cost - config.revoke_hazard_cost
                         + _brute_force_value(config, inputs, fail_next))
        )

    candidates.append(inputs.e_manual)
    best = max(candidates)
    if best < inputs.e_manual + config.stop_epsilon:
        best = inputs.e_manual
    return best


def _small_inputs(n_slots: int, p_success: tuple[float, ...]) -> PlanningInputs:
    assert len(p_success) == n_slots
    return PlanningInputs(
        amount=1000.0,
        p_success=p_success,
        e_manual=200.0,
        e_manual_late=50.0,
    )


def test_dp_matches_brute_force_enumeration_on_a_small_horizon() -> None:
    config = PlannerConfig(n_slots=4, max_attempts=2, notice_lead_slots=2)
    inputs = _small_inputs(4, (0.3, 0.5, 0.7, 0.4))
    result = solve(config, inputs)

    for b in range(config.max_attempts + 1):
        for n in range(NO_NOTICE, config.notice_lead_slots + 1):
            state = (0, b, n)
            expected = _brute_force_value(config, inputs, state)
            assert result.values[state] == pytest.approx(expected), (
                f"mismatch at root state {state}: dp={result.values[state]} "
                f"brute_force={expected}"
            )


def test_dp_matches_brute_force_at_every_reachable_state_not_just_root() -> None:
    config = PlannerConfig(n_slots=4, max_attempts=2, notice_lead_slots=2)
    inputs = _small_inputs(4, (0.6, 0.2, 0.9, 0.1))
    result = solve(config, inputs)

    for t in range(config.n_slots + 1):
        for b in range(config.max_attempts + 1):
            for n in range(NO_NOTICE, config.notice_lead_slots + 1):
                state = (t, b, n)
                expected = _brute_force_value(config, inputs, state)
                assert result.values[state] == pytest.approx(expected), state


def test_more_budget_never_lowers_root_value() -> None:
    config = PlannerConfig(n_slots=6, max_attempts=3, notice_lead_slots=2)
    inputs = _small_inputs(6, (0.4, 0.5, 0.3, 0.6, 0.2, 0.5))
    result = solve(config, inputs)
    values_by_budget = [result.root_value(b) for b in range(config.max_attempts + 1)]
    assert values_by_budget == sorted(values_by_budget)


def test_higher_success_probability_everywhere_never_lowers_root_value() -> None:
    config = PlannerConfig(n_slots=6, max_attempts=2, notice_lead_slots=2)
    low = _small_inputs(6, (0.1, 0.1, 0.1, 0.1, 0.1, 0.1))
    high = _small_inputs(6, (0.5, 0.5, 0.5, 0.5, 0.5, 0.5))
    result_low = solve(config, low)
    result_high = solve(config, high)
    assert result_high.root_value(2) >= result_low.root_value(2)


def test_zero_budget_never_chooses_attempt_anywhere() -> None:
    config = PlannerConfig(n_slots=6, max_attempts=2, notice_lead_slots=2)
    inputs = _small_inputs(6, (0.9, 0.9, 0.9, 0.9, 0.9, 0.9))
    result = solve(config, inputs)
    for t in range(config.n_slots):
        for n in range(NO_NOTICE, config.notice_lead_slots + 1):
            assert result.actions[(t, 0, n)] != ActionType.ATTEMPT


def test_overwhelmingly_generous_escalation_value_stops_immediately() -> None:
    config = PlannerConfig(n_slots=10, max_attempts=3, notice_lead_slots=2)
    inputs = PlanningInputs(
        amount=100.0,
        p_success=tuple([0.5] * 10),
        e_manual=1_000_000.0,  # escalation is absurdly more valuable than anything else
        e_manual_late=999_999.0,
    )
    result = solve(config, inputs)
    assert result.root_action(3) == ActionType.STOP_AND_ESCALATE


def test_revoked_mandate_style_zero_success_probability_prefers_stop_over_attempt() -> None:
    """A cause=MANDATE_REVOKED-style case: no attempt can ever succeed.
    The planner should never choose ATTEMPT once a notice is ready, since
    ATTEMPT's expected value there is strictly worse than IDLE/STOP."""
    config = PlannerConfig(n_slots=6, max_attempts=2, notice_lead_slots=2, revoke_hazard_cost=5.0)
    inputs = _small_inputs(6, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    result = solve(config, inputs)
    for t in range(config.n_slots):
        assert result.actions[(t, 2, NOTICE_READY)] != ActionType.ATTEMPT


def test_root_state_starts_with_no_notice_by_construction() -> None:
    config = PlannerConfig(n_slots=4, max_attempts=1, notice_lead_slots=2)
    inputs = _small_inputs(4, (0.5, 0.5, 0.5, 0.5))
    result = solve(config, inputs)
    assert (0, 1, NO_NOTICE) in result.values


def test_solve_rejects_mismatched_p_success_length() -> None:
    config = PlannerConfig(n_slots=4)
    inputs = _small_inputs(3, (0.5, 0.5, 0.5))  # wrong length for n_slots=4
    with pytest.raises(ValueError, match="p_success"):
        solve(config, inputs)


def test_planning_inputs_rejects_out_of_range_probability() -> None:
    with pytest.raises(ValueError, match="p_success"):
        PlanningInputs(amount=1.0, p_success=(1.5,), e_manual=0.0, e_manual_late=0.0)


def test_planner_config_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="n_slots"):
        PlannerConfig(n_slots=0)
    with pytest.raises(ValueError, match="notice_lead_slots"):
        PlannerConfig(notice_lead_slots=0)
    with pytest.raises(ValueError, match="stop_epsilon"):
        PlannerConfig(stop_epsilon=-1.0)


@given(
    n_slots=st.integers(min_value=2, max_value=8),
    max_attempts=st.integers(min_value=0, max_value=3),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_dp_matches_brute_force_across_random_small_configs(
    n_slots: int, max_attempts: int, seed: int
) -> None:
    rng = random.Random(seed)
    config = PlannerConfig(n_slots=n_slots, max_attempts=max_attempts, notice_lead_slots=2)
    inputs = PlanningInputs(
        amount=rng.uniform(100, 2000),
        p_success=tuple(rng.random() for _ in range(n_slots)),
        e_manual=rng.uniform(0, 500),
        e_manual_late=rng.uniform(0, 500),
    )
    result = solve(config, inputs)
    root = (0, max_attempts, NO_NOTICE)
    assert result.values[root] == pytest.approx(_brute_force_value(config, inputs, root))
