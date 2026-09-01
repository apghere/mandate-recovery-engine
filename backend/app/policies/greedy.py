"""GREEDY — ablation policy (docs §F.1's "10x difference test" row:
"Retry is modelled as budgeted optimal stopping rather than
classification"). Uses the identical calibrated success model MRE does,
but a naive greedy timing choice instead of the DP's multi-step
optimal-stopping formulation. Isolates what the *planning* is actually
worth: if MRE beats greedy on the benchmark, the value is in solving the
sequencing problem, not merely in having a calibrated probability at all.

For each remaining attempt, in order: pick whichever not-yet-used slot
still reachable (after the mandatory notice lead time) scores highest,
notify ahead of it, attempt there. Stops only via a one-step-lookahead
check — the single best remaining slot's naive expected value is worse
than escalating — never the DP's full multi-step comparison.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.domain.planner import PlannerConfig, PlanningInputs
from app.ml.inference import slot_datetime
from app.policies.fixed import ScheduledStep

POLICY_VERSION = "greedy-v1"


@dataclass(frozen=True)
class GreedyPlan:
    steps: list[ScheduledStep]
    immediate_stop: bool


def compute_greedy_schedule(
    *,
    start_date: date,
    attempts_remaining: int,
    config: PlannerConfig,
    inputs: PlanningInputs,
) -> GreedyPlan:
    success_value = inputs.amount + inputs.continuation_value
    lead = config.notice_lead_slots

    used = [False] * config.n_slots
    steps: list[ScheduledStep] = []
    attempts_left = attempts_remaining
    earliest_available = 0

    while attempts_left > 0:
        earliest_attemptable = earliest_available + lead
        best_t: int | None = None
        best_p = -1.0
        for t in range(earliest_attemptable, config.n_slots):
            if used[t]:
                continue
            if inputs.p_success[t] > best_p:
                best_p = inputs.p_success[t]
                best_t = t
        if best_t is None:
            break

        naive_value = best_p * success_value + (1 - best_p) * (
            -config.attempt_cost - config.revoke_hazard_cost
        )
        if naive_value < inputs.e_manual:
            if not steps:
                return GreedyPlan(steps=[], immediate_stop=True)
            break

        notify_t = best_t - lead
        attempt_at = slot_datetime(start_date, best_t)
        steps.append(
            ScheduledStep(
                "notify", slot_datetime(start_date, notify_t), covers_debit_at=attempt_at
            )
        )
        steps.append(ScheduledStep("attempt", attempt_at))
        used[best_t] = True
        earliest_available = best_t + 1
        attempts_left -= 1

    return GreedyPlan(steps=steps, immediate_stop=False)
