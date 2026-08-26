import pytest
from app.domain.fsm import Event, InvalidTransition, is_terminal, legal_events, transition
from app.domain.types import TERMINAL_STATES, CaseState
from hypothesis import given
from hypothesis import strategies as st


def test_happy_path_automatic_recovery() -> None:
    s = CaseState.DUE
    s = transition(s, Event.CYCLE_FAILED)
    s = transition(s, Event.CAUSE_NORMALIZED)
    s = transition(s, Event.PLAN_READY)
    s = transition(s, Event.STEP_DUE)
    s = transition(s, Event.ATTEMPT_SUCCEEDED)
    assert s == CaseState.RECOVERED


def test_happy_path_stop_and_escalate() -> None:
    s = CaseState.DUE
    s = transition(s, Event.CYCLE_FAILED)
    s = transition(s, Event.CAUSE_NORMALIZED)
    s = transition(s, Event.STOP_AND_ESCALATE)
    s = transition(s, Event.ESCALATED)
    s = transition(s, Event.MANUAL_SUCCEEDED)
    assert s == CaseState.RECOVERED


_TERMINAL_SORTED = sorted(TERMINAL_STATES, key=lambda s: s.value)


@given(state=st.sampled_from(_TERMINAL_SORTED), event=st.sampled_from(list(Event)))
def test_no_transition_ever_exits_a_terminal_state(state: CaseState, event: Event) -> None:
    with pytest.raises(InvalidTransition):
        transition(state, event)


@given(state=st.sampled_from(list(CaseState)), event=st.sampled_from(list(Event)))
def test_transition_is_pure_and_deterministic(state: CaseState, event: Event) -> None:
    try:
        first = transition(state, event)
    except InvalidTransition:
        with pytest.raises(InvalidTransition):
            transition(state, event)
        return
    assert transition(state, event) == first


def test_terminal_states_have_no_legal_events() -> None:
    for state in TERMINAL_STATES:
        assert legal_events(state) == frozenset()
        assert is_terminal(state)


def test_non_terminal_states_have_at_least_one_legal_event() -> None:
    for state in CaseState:
        if state not in TERMINAL_STATES:
            assert legal_events(state), f"{state} has no outgoing transitions"
