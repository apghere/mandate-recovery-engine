"""Event ingestion (FR-1, docs §G.2 M1).

Idempotent on events.external_id — the dedupe boundary is a Postgres UNIQUE
constraint (repo.insert_event's ON CONFLICT DO NOTHING), not a Python-side
check, so it holds even under concurrent duplicate delivery.

Design note on attempt #1 vs. the FSM: `mandate.cycle.due` only registers
bookkeeping (mandate + cycle in state DUE) — no plan. NPCI's "one initial
attempt plus up to three retries" means attempt #1 is fired by the
merchant's ordinary collection flow, *outside* MRE (docs §I.4's "Current
approach": "Fire debit at a fixed hour", no policy checked) — MRE only
engages once that first attempt's outcome is known:

  * `debit.succeeded` (seq 1): the cycle resolves before MRE's state
    machine is ever invoked. This deliberately does NOT go through
    `fsm.transition()` — DUE was never really an "engaged" FSM state for
    this cycle, it was just the bookkeeping default.
  * `debit.failed` (seq 1): THIS is what docs §G.1's "a merchant's failed
    mandate cycle arrives as an event" refers to, and what
    `domain/fsm.py`'s DUE -[CYCLE_FAILED]-> DIAGNOSING -> ... path models.
    MRE (or `fixed`/`greedy`) takes over from here for the remaining
    budget (seq 2-4).

Policy selection: `ingest_debit_failed` takes a `compute_plan` callback
((cycle due_date, normalized cause) -> PlanChoice), defaulting to the P0
fixed baseline. Taking the cause as an explicit argument (not just the
due_date) is deliberate: it's the *actually normalized* cause for *this*
case, computed a few lines above in this same function, and it's what
lets a cause like MANDATE_REVOKED genuinely drive the planner's own
near-zero probability estimate for this specific case — not a
stand-in a caller decided on in advance. This keeps ingest.py itself
agnostic about *which* policy produced a schedule, or how —
app/policies/live.py's select_compute_plan is what api/app.py's real
`/events` endpoint actually uses (payer/model context bound in per-case);
scripts/replay_compare.py and evaluation/runner.py build their own
closures for benchmark purposes and deliberately hold cause fixed instead
(see their own docstrings for why) rather than this module needing to
know anything about payers or trained models. A PlanChoice with
`immediate_stop=True` (the DP deciding, at the root, that no attempt is
worth making — docs §W2) routes straight to ESCALATING -> AWAITING_MANUAL
instead of
SCHEDULED, with zero further attempts consumed.

`mandate.revoked` / `notification.opted_out` (Phase 7, docs §N Day 4):
mandate-scoped, not cycle-scoped — a revoked mandate or an opt-out applies
to every cycle currently in flight for it, not just whichever cycle
happened to trigger the webhook. `_abandon_in_flight_cycles` resolves each
non-terminal cycle through the FSM's existing MANDATE_REVOKED/OPTED_OUT
edges (domain/fsm.py) immediately, rather than relying on the policy
gate to slowly deny its way through every remaining plan_step one tick at
a time and eventually get swept by `worker.sweep_exhausted_plans` under
the dishonest label "plan_exhausted".

Out-of-order delivery (docs §M.1's chaos matrix — webhooks are
at-least-once, NOT ordered): a debit outcome for a cycle_id ingestion has
never seen a mandate.cycle.due for raises `UnknownCycleError`, a clean,
retryable signal — not a leaked FK-violation or an AssertionError. Because
the check happens inside the same `with conn.transaction()` block as the
event insert, raising here rolls back that insert too, so a retry after
the real cycle.due event lands re-attempts cleanly instead of being
silently treated as a false duplicate.

Note on transactions: `conn.transaction()` on a non-autocommit connection
(psycopg3's default, which this app uses throughout) only opens a
SAVEPOINT-scoped nested block — it does not by itself commit the ambient
transaction. Every function here therefore calls `conn.commit()` exactly
once, after its `with conn.transaction():` block, so writes are actually
durable and visible to other connections.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app import repo
from app.ai.normalizer import normalize
from app.db import Conn
from app.domain.fsm import Event, legal_events, transition
from app.domain.types import CaseState, Cause
from app.policies.fixed import POLICY_VERSION as FIXED_POLICY_VERSION
from app.policies.fixed import ScheduledStep, compute_fixed_schedule
from data.generator import load_taxonomy

_TAXONOMY = load_taxonomy()


class UnknownCycleError(Exception):
    """A debit outcome event named a cycle_id that no mandate.cycle.due has
    ever registered. See module docstring's "Out-of-order delivery" note."""

    def __init__(self, cycle_id: str) -> None:
        super().__init__(
            f"unknown cycle_id={cycle_id!r} — event arrived before mandate.cycle.due"
        )
        self.cycle_id = cycle_id


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    duplicate: bool


@dataclass(frozen=True)
class CycleDueEvent:
    external_id: str
    mandate_id: str
    cycle_id: str
    merchant_id: str
    payer_id: str
    rail: str
    issuer_code: str
    amount: float
    due_date: date
    occurred_at: datetime


def ingest_cycle_due(conn: Conn, event: CycleDueEvent) -> IngestResult:
    with conn.transaction():
        payload: dict[str, Any] = {
            "merchant_id": event.merchant_id,
            "payer_id": event.payer_id,
            "amount": event.amount,
            "due_date": event.due_date.isoformat(),
        }
        inserted = repo.insert_event(
            conn,
            external_id=event.external_id,
            event_type="mandate.cycle.due",
            mandate_id=event.mandate_id,
            cycle_id=event.cycle_id,
            occurred_at=event.occurred_at,
            payload=payload,
        )
        if inserted:
            repo.upsert_mandate(
                conn,
                mandate_id=event.mandate_id,
                merchant_id=event.merchant_id,
                payer_id=event.payer_id,
                rail=event.rail,
                max_amount=event.amount,
                status="active",
                issuer_code=event.issuer_code,
            )
            repo.create_cycle(
                conn,
                cycle_id=event.cycle_id,
                mandate_id=event.mandate_id,
                due_date=event.due_date,
                amount=event.amount,
                state=CaseState.DUE.value,
            )
    conn.commit()
    return IngestResult(accepted=inserted, duplicate=not inserted)


@dataclass(frozen=True)
class DebitOutcomeEvent:
    external_id: str
    mandate_id: str
    cycle_id: str
    occurred_at: datetime
    amount: float
    raw_reason: str | None = None  # only meaningful on failure


def ingest_debit_succeeded(conn: Conn, event: DebitOutcomeEvent) -> IngestResult:
    """First attempt (seq 1) succeeded — resolved before MRE ever engages."""
    with conn.transaction():
        inserted = repo.insert_event(
            conn,
            external_id=event.external_id,
            event_type="debit.succeeded",
            mandate_id=event.mandate_id,
            cycle_id=event.cycle_id,
            occurred_at=event.occurred_at,
            payload={"amount": event.amount, "sequence_no": 1},
        )
        if inserted:
            if repo.get_cycle(conn, event.cycle_id) is None:
                raise UnknownCycleError(event.cycle_id)
            intent_id = repo.reserve_attempt_intent(
                conn,
                cycle_id=event.cycle_id,
                sequence_no=1,
                idempotency_key=f"{event.cycle_id}:seq1:{event.external_id}",
                scheduled_for=event.occurred_at,
            )
            repo.update_attempt_outcome(
                conn, intent_id, outcome="success", raw_reason=None, executed_at=event.occurred_at
            )
            repo.update_cycle_state(
                conn,
                event.cycle_id,
                state=CaseState.RECOVERED.value,
                attempts_used=1,
                recovered_amount=event.amount,
                closed_at=event.occurred_at,
            )
            repo.insert_audit(
                conn,
                actor="system",
                cycle_id=event.cycle_id,
                action="first_attempt_succeeded",
                detail={"attempt_intent_id": intent_id},
            )
    conn.commit()
    return IngestResult(accepted=inserted, duplicate=not inserted)


@dataclass(frozen=True)
class PlanChoice:
    policy_version: str
    steps: list[ScheduledStep] = field(default_factory=list)
    immediate_stop: bool = False
    expected_value: float = 0.0
    solver_ms: float = 0.0


def _default_fixed_plan(due_date: date, _cause: Cause) -> PlanChoice:
    return PlanChoice(
        policy_version=FIXED_POLICY_VERSION,
        steps=compute_fixed_schedule(due_date),
        immediate_stop=False,
    )


def ingest_debit_failed(
    conn: Conn,
    event: DebitOutcomeEvent,
    *,
    compute_plan: Callable[[date, Cause], PlanChoice] = _default_fixed_plan,
) -> IngestResult:
    """First attempt (seq 1) failed — the chosen policy engages for the
    remaining budget. See module docstring for `compute_plan`."""
    with conn.transaction():
        inserted = repo.insert_event(
            conn,
            external_id=event.external_id,
            event_type="debit.failed",
            mandate_id=event.mandate_id,
            cycle_id=event.cycle_id,
            occurred_at=event.occurred_at,
            payload={"amount": event.amount, "sequence_no": 1, "raw_reason": event.raw_reason},
        )
        if inserted:
            cycle = repo.get_cycle(conn, event.cycle_id)
            if cycle is None:
                raise UnknownCycleError(event.cycle_id)
            mandate = repo.get_mandate(conn, event.mandate_id)
            assert mandate is not None  # FK from cycles.mandate_id guarantees this

            intent_id = repo.reserve_attempt_intent(
                conn,
                cycle_id=event.cycle_id,
                sequence_no=1,
                idempotency_key=f"{event.cycle_id}:seq1:{event.external_id}",
                scheduled_for=event.occurred_at,
            )

            # Decline-string normalisation (docs §K.2): dictionary -> fuzzy
            # -> LLM -> UNKNOWN. Never raises; UNKNOWN is a correct, first-
            # class outcome under genuine uncertainty, not a failure.
            normalization = (
                normalize(
                    event.raw_reason,
                    issuer_code=mandate["issuer_code"],
                    rail=mandate["rail"],
                    taxonomy=_TAXONOMY,
                )
                if event.raw_reason is not None
                else None
            )
            repo.update_attempt_outcome(
                conn,
                intent_id,
                outcome="failure",
                raw_reason=event.raw_reason,
                executed_at=event.occurred_at,
                canonical_cause=normalization.cause.value if normalization else None,
                cause_confidence=normalization.confidence if normalization else None,
                cause_source=normalization.source if normalization else None,
            )

            repo.insert_audit(
                conn,
                actor="system",
                cycle_id=event.cycle_id,
                action="cause_normalized",
                detail={
                    "attempt_intent_id": intent_id,
                    "raw_reason": event.raw_reason,
                    "cause": normalization.cause.value if normalization else None,
                    "confidence": normalization.confidence if normalization else None,
                    "source": normalization.source if normalization else None,
                },
            )

            plan_choice = compute_plan(
                cycle["due_date"], normalization.cause if normalization else Cause.UNKNOWN
            )

            state = CaseState.DUE
            state = transition(state, Event.CYCLE_FAILED)
            state = transition(state, Event.CAUSE_NORMALIZED)

            if plan_choice.immediate_stop:
                state = transition(state, Event.STOP_AND_ESCALATE)
                state = transition(state, Event.ESCALATED)
                assert state == CaseState.AWAITING_MANUAL
                repo.update_cycle_state(conn, event.cycle_id, state=state.value, attempts_used=1)
                repo.insert_audit(
                    conn,
                    actor="system",
                    cycle_id=event.cycle_id,
                    action="stopped_and_escalated",
                    detail={
                        "policy": plan_choice.policy_version,
                        "expected_value": plan_choice.expected_value,
                        "first_attempt_intent_id": intent_id,
                    },
                )
            else:
                state = transition(state, Event.PLAN_READY)
                assert state == CaseState.SCHEDULED
                repo.update_cycle_state(conn, event.cycle_id, state=state.value, attempts_used=1)

                plan_id = repo.insert_plan(
                    conn,
                    cycle_id=event.cycle_id,
                    model_version=plan_choice.policy_version,
                    feature_hash="n/a",
                    expected_value=plan_choice.expected_value,
                    stop_reason=None,
                    solver_ms=int(round(plan_choice.solver_ms)),
                )
                for step in plan_choice.steps:
                    repo.insert_plan_step(
                        conn,
                        plan_id=plan_id,
                        step_type=step.step_type,
                        scheduled_for=step.scheduled_for,
                        covers_debit_at=step.covers_debit_at,
                    )
                repo.insert_audit(
                    conn,
                    actor="system",
                    cycle_id=event.cycle_id,
                    action="plan_created",
                    detail={
                        "plan_id": plan_id,
                        "policy": plan_choice.policy_version,
                        "first_attempt_intent_id": intent_id,
                    },
                )
    conn.commit()
    return IngestResult(accepted=inserted, duplicate=not inserted)


@dataclass(frozen=True)
class MandateLifecycleEvent:
    external_id: str
    mandate_id: str
    occurred_at: datetime


def ingest_mandate_revoked(conn: Conn, event: MandateLifecycleEvent) -> IngestResult:
    """The mandate itself is gone (issuer/NPCI notified us) — permanent,
    mandate-scoped, not a per-cycle decline. See module docstring."""
    with conn.transaction():
        inserted = repo.insert_event(
            conn,
            external_id=event.external_id,
            event_type="mandate.revoked",
            mandate_id=event.mandate_id,
            cycle_id=None,
            occurred_at=event.occurred_at,
            payload={},
        )
        if inserted:
            repo.set_mandate_status(conn, event.mandate_id, status="revoked")
            _abandon_in_flight_cycles(
                conn,
                event.mandate_id,
                fsm_event=Event.MANDATE_REVOKED,
                action="mandate_revoked",
                closed_at=event.occurred_at,
            )
    conn.commit()
    return IngestResult(accepted=inserted, duplicate=not inserted)


def ingest_notification_opted_out(conn: Conn, event: MandateLifecycleEvent) -> IngestResult:
    """Payer used an opt-out mechanism. Mandate-scoped, matching
    mandates.opted_out — every in-flight cycle on this mandate stops being
    contacted/attempted, not just whichever cycle triggered the webhook."""
    with conn.transaction():
        inserted = repo.insert_event(
            conn,
            external_id=event.external_id,
            event_type="notification.opted_out",
            mandate_id=event.mandate_id,
            cycle_id=None,
            occurred_at=event.occurred_at,
            payload={},
        )
        if inserted:
            repo.set_mandate_opted_out(conn, event.mandate_id, opted_out=True)
            _abandon_in_flight_cycles(
                conn,
                event.mandate_id,
                fsm_event=Event.OPTED_OUT,
                action="opted_out",
                closed_at=event.occurred_at,
            )
    conn.commit()
    return IngestResult(accepted=inserted, duplicate=not inserted)


def _abandon_in_flight_cycles(
    conn: Conn, mandate_id: str, *, fsm_event: Event, action: str, closed_at: datetime
) -> None:
    for cycle in repo.non_terminal_cycles_for_mandate(conn, mandate_id):
        current_state = CaseState(cycle["state"])
        if fsm_event not in legal_events(current_state):
            # DUE/DIAGNOSING/PLANNING/ESCALATING are momentary states a
            # concurrent transaction essentially never observes mid-flight
            # (docs §H.3 — each is entered and exited within one function
            # call); AWAITING_MANUAL + MANDATE_REVOKED has no FSM edge on
            # purpose (domain/fsm.py) — a human is already handling it, so a
            # later revocation doesn't silently override that in progress.
            # The mandate-level flag change above is still real and durable
            # even when no cycle-level transition applies here.
            continue
        new_state = transition(current_state, fsm_event)
        repo.update_cycle_state(conn, cycle["id"], state=new_state.value, closed_at=closed_at)
        repo.cancel_pending_steps_for_cycle(conn, cycle["id"], reason=action)
        repo.insert_audit(
            conn,
            actor="system",
            cycle_id=cycle["id"],
            action=action,
            detail={"previous_state": current_state.value},
        )
