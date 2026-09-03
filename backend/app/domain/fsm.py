"""Recovery-case state machine (docs P.3). Pure: no I/O, no clock, no network."""
from __future__ import annotations

from enum import StrEnum

from app.domain.types import TERMINAL_STATES, CaseState


class Event(StrEnum):
    CYCLE_FAILED = "CYCLE_FAILED"
    CAUSE_NORMALIZED = "CAUSE_NORMALIZED"
    PLAN_READY = "PLAN_READY"
    STOP_AND_ESCALATE = "STOP_AND_ESCALATE"
    STEP_DUE = "STEP_DUE"
    ATTEMPT_FAILED_BUDGET_LEFT = "ATTEMPT_FAILED_BUDGET_LEFT"
    ATTEMPT_SUCCEEDED = "ATTEMPT_SUCCEEDED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    OPTED_OUT = "OPTED_OUT"
    ESCALATED = "ESCALATED"
    MANUAL_SUCCEEDED = "MANUAL_SUCCEEDED"
    MANUAL_ABANDONED = "MANUAL_ABANDONED"


class InvalidTransition(ValueError):
    def __init__(self, state: CaseState, event: Event) -> None:
        super().__init__(f"no transition from {state.value} on {event.value}")
        self.state = state
        self.event = event


# (current_state, event) -> next_state. Absence of an entry means the
# transition is illegal — this is what makes "no exit from a terminal
# state" true by construction rather than by a runtime check.
_TRANSITIONS: dict[tuple[CaseState, Event], CaseState] = {
    (CaseState.DUE, Event.CYCLE_FAILED): CaseState.DIAGNOSING,
    (CaseState.DIAGNOSING, Event.CAUSE_NORMALIZED): CaseState.PLANNING,
    (CaseState.PLANNING, Event.PLAN_READY): CaseState.SCHEDULED,
    (CaseState.PLANNING, Event.STOP_AND_ESCALATE): CaseState.ESCALATING,
    (CaseState.SCHEDULED, Event.STEP_DUE): CaseState.EXECUTING,
    (CaseState.SCHEDULED, Event.BUDGET_EXHAUSTED): CaseState.ABANDONED,
    (CaseState.SCHEDULED, Event.MANDATE_REVOKED): CaseState.ABANDONED,
    (CaseState.SCHEDULED, Event.OPTED_OUT): CaseState.ABANDONED,
    (CaseState.SCHEDULED, Event.STOP_AND_ESCALATE): CaseState.ESCALATING,
    (CaseState.EXECUTING, Event.ATTEMPT_FAILED_BUDGET_LEFT): CaseState.SCHEDULED,
    (CaseState.EXECUTING, Event.ATTEMPT_SUCCEEDED): CaseState.RECOVERED,
    (CaseState.EXECUTING, Event.BUDGET_EXHAUSTED): CaseState.ABANDONED,
    (CaseState.EXECUTING, Event.MANDATE_REVOKED): CaseState.ABANDONED,
    (CaseState.EXECUTING, Event.OPTED_OUT): CaseState.ABANDONED,
    (CaseState.ESCALATING, Event.ESCALATED): CaseState.AWAITING_MANUAL,
    (CaseState.AWAITING_MANUAL, Event.MANUAL_SUCCEEDED): CaseState.RECOVERED,
    (CaseState.AWAITING_MANUAL, Event.MANUAL_ABANDONED): CaseState.ABANDONED,
    (CaseState.AWAITING_MANUAL, Event.OPTED_OUT): CaseState.ABANDONED,
}


def transition(state: CaseState, event: Event) -> CaseState:
    """Pure transition function. Raises InvalidTransition on any illegal edge,
    including every attempted exit from a terminal state."""
    key = (state, event)
    if key not in _TRANSITIONS:
        raise InvalidTransition(state, event)
    return _TRANSITIONS[key]


def is_terminal(state: CaseState) -> bool:
    return state in TERMINAL_STATES


def legal_events(state: CaseState) -> frozenset[Event]:
    return frozenset(event for (s, event) in _TRANSITIONS if s == state)
