"""The planner (docs §K.4) — the actual product. Exact backward induction
over (slot, attempts-remaining, notice-state). Pure: no I/O, no clock, no
network — probabilities and costs are inputs, not computed here (that's
ml/ for probabilities, and, eventually, a hazard model for opt-out/
revocation).

Notice state is a deliberate simplification of the real system's exact
per-debit notice matching (domain/policy.py's `_notice_covers_debit`): a
single integer tracks "slots until the most recently sent, unconsumed
notice becomes valid" (-1 = none outstanding, 0 = ready now, >0 = still
within the required lead time). No expiry is modelled — once ready, a
notice stays usable until consumed by an ATTEMPT. This is fine precisely
*because* the real `authorize()` independently re-enforces the true
7-day cap at execution time regardless of what the planner assumed (docs
§H.2) — a planner/reality mismatch here surfaces as docs §W3's "notice
failure -> debit denied -> plan re-solves" scenario, not as a silent bug.

Revocation and opt-out hazards are modelled as fixed per-action costs,
not state- or history-dependent functions — docs §N.7's cut-order item #5
explicitly permits this ("Revocation hazard model -> fixed constant...
rescues the project" at half a day's cost). The real hazard model
(logistic regression over annoyance features, docs §K.3) is Phase 6/7
scope.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.types import ActionType

State = tuple[int, int, int]  # (slot, attempts_remaining, notice_state)

NO_NOTICE = -1
NOTICE_READY = 0


@dataclass(frozen=True)
class PlannerConfig:
    n_slots: int = 28
    max_attempts: int = 4
    notice_lead_slots: int = 2
    notify_cost: float = 1.0
    attempt_cost: float = 0.0
    optout_hazard_cost: float = 5.0
    revoke_hazard_cost: float = 20.0
    # Stopping rule (docs §K.4): stop when the best achievable value from
    # continuing falls below E_manual + epsilon. Not a separate tuned
    # threshold — the DP's own root-node (and every state's) comparison.
    # Default 0 is a pure argmax; a small positive value additionally
    # prefers stopping on near-ties, matching "decisive" framing in §R.
    stop_epsilon: float = 0.0

    def __post_init__(self) -> None:
        if self.n_slots < 1:
            raise ValueError("n_slots must be >= 1")
        if self.notice_lead_slots < 1:
            raise ValueError("notice_lead_slots must be >= 1")
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be >= 0")
        if self.stop_epsilon < 0:
            raise ValueError("stop_epsilon must be >= 0")


@dataclass(frozen=True)
class PlanningInputs:
    amount: float
    p_success: tuple[float, ...]  # calibrated P(success | ATTEMPT at slot t), len == n_slots
    e_manual: float  # value of escalating to the human-assisted rail right now
    e_manual_late: float  # value of escalating once the horizon is exhausted
    continuation_value: float = 0.0  # gamma * expected future mandate value; 0 = this cycle only

    def __post_init__(self) -> None:
        for p in self.p_success:
            if not 0.0 <= p <= 1.0:
                raise ValueError(f"p_success entries must be in [0, 1], got {p}")


@dataclass(frozen=True)
class SolveResult:
    config: PlannerConfig
    values: dict[State, float]
    actions: dict[State, ActionType]

    def root_value(self, attempts_remaining: int) -> float:
        return self.values[(0, attempts_remaining, NO_NOTICE)]

    def root_action(self, attempts_remaining: int) -> ActionType:
        return self.actions[(0, attempts_remaining, NO_NOTICE)]


def notice_states(config: PlannerConfig) -> range:
    return range(NO_NOTICE, config.notice_lead_slots + 1)


def advance_notice(notice_state: int, *, sent_now: bool, config: PlannerConfig) -> int:
    if sent_now:
        return config.notice_lead_slots
    if notice_state == NO_NOTICE:
        return NO_NOTICE
    if notice_state > NOTICE_READY:
        return notice_state - 1
    return NOTICE_READY


def solve(config: PlannerConfig, inputs: PlanningInputs) -> SolveResult:
    if len(inputs.p_success) != config.n_slots:
        raise ValueError("p_success must have exactly config.n_slots entries")

    success_value = inputs.amount + inputs.continuation_value

    values: dict[State, float] = {}
    actions: dict[State, ActionType] = {}

    for b in range(config.max_attempts + 1):
        for n in notice_states(config):
            values[(config.n_slots, b, n)] = inputs.e_manual_late
            actions[(config.n_slots, b, n)] = ActionType.STOP_AND_ESCALATE

    for t in range(config.n_slots - 1, -1, -1):
        p = inputs.p_success[t]
        for b in range(config.max_attempts + 1):
            for n in notice_states(config):
                # Listed in tie-break priority order (most to least
                # decisive): on an exact value tie, max()'s first-wins
                # behaviour then prefers ATTEMPT > STOP_AND_ESCALATE >
                # NOTIFY > IDLE, rather than an arbitrary artifact of
                # insertion order. This matters concretely whenever
                # e_manual is time-invariant: IDLE-until-later-STOP and
                # STOP-now are then mathematically identical in value, and
                # picking STOP-now over drifting is the better story for
                # an audit trail even though the EV is unchanged either
                # way.
                candidates: list[tuple[float, ActionType]] = []

                if b > 0 and n == NOTICE_READY:
                    fail_next = (t + 1, b - 1, NO_NOTICE)
                    attempt_value = p * success_value + (1 - p) * (
                        -config.attempt_cost - config.revoke_hazard_cost + values[fail_next]
                    )
                    candidates.append((attempt_value, ActionType.ATTEMPT))

                candidates.append((inputs.e_manual, ActionType.STOP_AND_ESCALATE))

                if n == NO_NOTICE:
                    notify_next = (t + 1, b, advance_notice(n, sent_now=True, config=config))
                    notify_value = (
                        -config.notify_cost - config.optout_hazard_cost + values[notify_next]
                    )
                    candidates.append((notify_value, ActionType.NOTIFY))

                idle_next = (t + 1, b, advance_notice(n, sent_now=False, config=config))
                candidates.append((values[idle_next], ActionType.IDLE))

                best_value, best_action = max(candidates, key=lambda c: c[0])

                if best_action != ActionType.STOP_AND_ESCALATE and (
                    best_value < inputs.e_manual + config.stop_epsilon
                ):
                    best_value, best_action = inputs.e_manual, ActionType.STOP_AND_ESCALATE

                values[(t, b, n)] = best_value
                actions[(t, b, n)] = best_action

    return SolveResult(config=config, values=values, actions=actions)
