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
(cycle due_date -> PlanChoice), defaulting to the P0 fixed baseline. This
keeps ingest.py itself agnostic about *which* policy produced a schedule,
or how — app/policies/mre.py's and app/policies/greedy.py's callers build
a closure with payer/model context already bound in (see
scripts/replay_mre.py) rather than this module needing to know anything
about payers or trained models. A PlanChoice with `immediate_stop=True`
(the DP deciding, at the root, that no attempt is worth making — docs
§W2) routes straight to ESCALATING -> AWAITING_MANUAL instead of
SCHEDULED, with zero further attempts consumed.

`mandate.revoked` / `notification.opted_out` ingestion is deferred to the
Day-4 safety/chaos work (docs §N Day 4) where they're exercised directly.

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
from app.db import Conn
from app.domain.fsm import Event, transition
from app.domain.types import CaseState
from app.policies.fixed import POLICY_VERSION as FIXED_POLICY_VERSION
from app.policies.fixed import ScheduledStep, compute_fixed_schedule


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


def _default_fixed_plan(due_date: date) -> PlanChoice:
    return PlanChoice(
        policy_version=FIXED_POLICY_VERSION,
        steps=compute_fixed_schedule(due_date),
        immediate_stop=False,
    )


def ingest_debit_failed(
    conn: Conn,
    event: DebitOutcomeEvent,
    *,
    compute_plan: Callable[[date], PlanChoice] = _default_fixed_plan,
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
            intent_id = repo.reserve_attempt_intent(
                conn,
                cycle_id=event.cycle_id,
                sequence_no=1,
                idempotency_key=f"{event.cycle_id}:seq1:{event.external_id}",
                scheduled_for=event.occurred_at,
            )
            repo.update_attempt_outcome(
                conn,
                intent_id,
                outcome="failure",
                raw_reason=event.raw_reason,
                executed_at=event.occurred_at,
            )

            cycle = repo.get_cycle(conn, event.cycle_id)
            assert cycle is not None
            plan_choice = compute_plan(cycle["due_date"])

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
