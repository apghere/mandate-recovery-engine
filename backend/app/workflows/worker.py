"""Worker loop: processes due plan_steps, drains the outbox (docs §H.1, H.3).

Two-phase per attempt, on purpose (docs §H.3 — "the single most important
ordering rule"): (1) `process_due_plan_steps` authorizes and, if allowed,
*reserves* the attempt_intents row and an outbox row in the same
transaction; (2) `drain_outbox` is what actually calls the rail. A crash
between (1) and (2) leaves the attempt durably reserved and the outbox
entry undelivered — redelivery on the next tick is what makes this
idempotent rather than lossy or duplicating.

Both functions are meant to be called repeatedly (a real long-running
worker loop, or a replay driver advancing a simulated clock) — neither
blocks or sleeps itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app import repo
from app.adapters.simulator_client import RailDenied, SimulatorClient
from app.ai.notice import NoticeVariables, generate_notice
from app.db import Conn
from app.domain.fsm import Event, transition
from app.domain.policy import authorize
from app.domain.types import (
    AFA_THRESHOLD_DEFAULT,
    MAX_ATTEMPTS,
    ActionType,
    CaseSnapshot,
    CaseState,
    NoticeRecord,
)

# Phase 3 simplifications, documented rather than silently assumed: no
# per-merchant contact-cap config, no quiet-hours calendar, no kill-switch
# admin surface, and no AFA consent flow yet. Each is a real mechanism in
# domain/policy.py already — these constants just haven't been promoted to
# real config/state. Revisit alongside the Day 4 safety work.
CONTACT_CAP_DEFAULT = 3
OUTBOX_RETRY_BACKOFF_SECONDS = 30


def _build_snapshot(
    conn: Conn, cycle: dict[str, Any], mandate: dict[str, Any], now: datetime
) -> CaseSnapshot:
    notice_rows = repo.notices_covering(conn, cycle["id"])
    notices = tuple(
        NoticeRecord(sent_at=r["sent_at"], covers_debit_at=r["covers_debit_at"])
        for r in notice_rows
    )
    contact_count_today = sum(1 for r in notice_rows if r["sent_at"].date() == now.date())
    return CaseSnapshot(
        state=CaseState(cycle["state"]),
        attempts_used=repo.count_attempts(conn, cycle["id"]),
        mandate_active=mandate["status"] == "active",
        opted_out=bool(mandate["opted_out"]),
        amount=int(round(float(cycle["amount"]))),
        afa_threshold=AFA_THRESHOLD_DEFAULT,
        afa_satisfied=False,  # no AFA consent flow modelled yet — documented above
        notices=notices,
        contact_count_today=contact_count_today,
        contact_cap=CONTACT_CAP_DEFAULT,
        quiet_hours_active=False,
        merchant_kill_switch=False,
        global_kill_switch=False,
    )


@dataclass(frozen=True)
class TickResult:
    notified: int = 0
    notify_denied: int = 0
    dispatched: int = 0
    attempt_denied: int = 0


def sweep_exhausted_plans(conn: Conn, *, now: datetime) -> int:
    """Call once per tick, after `drain_outbox` — resolves cycles whose
    plan has run out of pending/dispatched steps without ever reaching
    RECOVERED (see repo.scheduled_cycles_with_no_remaining_steps). Reuses
    the existing (SCHEDULED, BUDGET_EXHAUSTED) -> ABANDONED edge: the
    persisted audit action name is honest about *why* even when the FSM
    event name doesn't literally mean "all 4 attempts were consumed"."""
    rows = repo.scheduled_cycles_with_no_remaining_steps(conn)
    for row in rows:
        new_state = transition(CaseState.SCHEDULED, Event.BUDGET_EXHAUSTED)
        repo.update_cycle_state(conn, row["id"], state=new_state.value, closed_at=now)
        repo.insert_audit(
            conn,
            actor="system",
            cycle_id=row["id"],
            action="plan_exhausted",
            detail={
                "attempts_used": row["attempts_used"],
                "reason": "no remaining planned actions; cycle never recovered",
            },
        )
        conn.commit()
    return len(rows)


def process_due_plan_steps(conn: Conn, *, now: datetime, limit: int = 200) -> TickResult:
    result = TickResult()
    steps = repo.fetch_due_plan_steps(conn, now=now, limit=limit)
    for step in steps:
        cycle = repo.get_cycle(conn, step["cycle_id"])
        assert cycle is not None
        mandate = repo.get_mandate(conn, cycle["mandate_id"])
        assert mandate is not None
        snapshot = _build_snapshot(conn, cycle, mandate, now)

        if step["step_type"] == "notify":
            result = _process_notify_step(conn, step, cycle, mandate, snapshot, now, result)
        elif step["step_type"] == "attempt":
            result = _process_attempt_step(conn, step, cycle, snapshot, now, result)
        else:  # pragma: no cover - escalate lands in a later phase
            repo.mark_plan_step(
                conn, step["id"], status="cancelled", cancelled_reason="unhandled_step_type"
            )
        # Commit per step, not per batch: each step is one crash-safe unit
        # of work (mirrors the outbox pattern's atomicity, docs §H.3), and
        # releases that row's SKIP LOCKED lock promptly rather than holding
        # every lock in the batch for the whole tick's duration.
        conn.commit()
    return result


def _process_notify_step(
    conn: Conn,
    step: dict[str, Any],
    cycle: dict[str, Any],
    mandate: dict[str, Any],
    snapshot: CaseSnapshot,
    now: datetime,
    result: TickResult,
) -> TickResult:
    verdict = authorize(ActionType.NOTIFY, snapshot, now)
    repo.insert_decision(
        conn,
        cycle_id=cycle["id"],
        action="NOTIFY",
        verdict="allow" if verdict.allowed else "deny",
        reason_code=verdict.reason_code.value if verdict.reason_code else None,
        policy_version="v1",
        input_snapshot={"contact_count_today": snapshot.contact_count_today},
    )
    if not verdict.allowed:
        assert verdict.reason_code is not None
        repo.mark_plan_step(
            conn, step["id"], status="cancelled", cancelled_reason=verdict.reason_code.value
        )
        repo.insert_audit(
            conn,
            actor="system",
            cycle_id=cycle["id"],
            action="notify_denied",
            detail={"reason": verdict.reason_code.value},
        )
        return TickResult(
            result.notified, result.notify_denied + 1, result.dispatched, result.attempt_denied
        )

    # The exact attempt this notice covers, set by whichever policy built
    # the plan (docs §I.10's freshness check is an exact covers_debit_at
    # match, not "within N days of the most recent notify" — see
    # app/policies/fixed.py's ScheduledStep docstring for why this can't
    # be assumed as a fixed offset for every policy). A NOTIFY step with
    # no covers_debit_at means the plan notified but the DP never actually
    # scheduled the attempt it was for (e.g. it decided to stop right
    # after) — nothing to cover, so no notification is sent.
    covers_debit_at = step["covers_debit_at"]
    if covers_debit_at is None:
        repo.mark_plan_step(conn, step["id"], status="done")
        repo.insert_audit(
            conn, actor="system", cycle_id=cycle["id"], action="notify_orphaned",
            detail={"reason": "no attempt was ultimately scheduled for this notice"},
        )
        return TickResult(
            result.notified, result.notify_denied, result.dispatched, result.attempt_denied
        )

    # docs §K.5: the LLM drafts, the deterministic validator decides. Falls
    # to a static, self-consistent template if the LLM is unavailable or
    # never produces a valid draft — never an unvalidated body persisted.
    notice_result = generate_notice(
        NoticeVariables(
            merchant_name=mandate["merchant_id"],  # no separate display name in schema yet
            amount=f"Rs.{cycle['amount']}",
            debit_date=covers_debit_at.strftime("%d %B %Y"),
            debit_time=covers_debit_at.strftime("%H:%M"),
            mandate_ref=cycle["mandate_id"],
            reason="recurring mandate payment",
            channel="sms",
        )
    )
    repo.insert_notification(
        conn,
        cycle_id=cycle["id"],
        sent_at=step["scheduled_for"],
        covers_debit_at=covers_debit_at,
        channel="sms",
        body=notice_result.body,
        generated_by=notice_result.generated_by,
        validator_result={
            "valid": notice_result.validator_result.valid,
            "errors": notice_result.validator_result.errors,
            "repaired": notice_result.repaired,
        },
    )
    repo.mark_plan_step(conn, step["id"], status="done")
    repo.insert_audit(conn, actor="system", cycle_id=cycle["id"], action="notify_sent", detail={})
    return TickResult(
        result.notified + 1, result.notify_denied, result.dispatched, result.attempt_denied
    )


def _process_attempt_step(
    conn: Conn,
    step: dict[str, Any],
    cycle: dict[str, Any],
    snapshot: CaseSnapshot,
    now: datetime,
    result: TickResult,
) -> TickResult:
    current_state = CaseState(cycle["state"])
    if current_state != CaseState.SCHEDULED:
        repo.mark_plan_step(
            conn, step["id"], status="cancelled", cancelled_reason="cycle_not_scheduled"
        )
        return result

    verdict = authorize(ActionType.ATTEMPT, snapshot, now, target_time=step["scheduled_for"])
    repo.insert_decision(
        conn,
        cycle_id=cycle["id"],
        action="ATTEMPT",
        verdict="allow" if verdict.allowed else "deny",
        reason_code=verdict.reason_code.value if verdict.reason_code else None,
        policy_version="v1",
        input_snapshot={"attempts_used": snapshot.attempts_used},
    )
    if not verdict.allowed:
        assert verdict.reason_code is not None
        # Not consumed (docs §M.1): the plan_step is cancelled, but no
        # attempt_intents row is ever reserved, and the cycle stays
        # SCHEDULED — the next pre-scheduled step still gets its chance.
        repo.mark_plan_step(
            conn, step["id"], status="cancelled", cancelled_reason=verdict.reason_code.value
        )
        repo.insert_audit(
            conn,
            actor="system",
            cycle_id=cycle["id"],
            action="attempt_denied",
            detail={"reason": verdict.reason_code.value},
        )
        return TickResult(
            result.notified, result.notify_denied, result.dispatched, result.attempt_denied + 1
        )

    new_state = transition(current_state, Event.STEP_DUE)
    repo.update_cycle_state(conn, cycle["id"], state=new_state.value)

    sequence_no = snapshot.attempts_used + 1
    idempotency_key = f"{cycle['id']}:seq{sequence_no}"
    intent_id = repo.reserve_attempt_intent(
        conn,
        cycle_id=cycle["id"],
        sequence_no=sequence_no,
        idempotency_key=idempotency_key,
        scheduled_for=step["scheduled_for"],
    )
    outbox_payload: dict[str, Any] = {
        "cycle_id": cycle["id"],
        "sequence_no": sequence_no,
        "idempotency_key": idempotency_key,
        "mandate_id": cycle["mandate_id"],
        "payer_id": None,
        "amount": float(cycle["amount"]),
        "scheduled_for": step["scheduled_for"].isoformat(),
        "plan_step_id": step["id"],
        "attempt_intent_id": intent_id,
    }
    repo.insert_outbox(
        conn, destination="simulator", idempotency_key=idempotency_key, payload=outbox_payload
    )
    repo.mark_plan_step(conn, step["id"], status="dispatched")
    repo.insert_audit(
        conn,
        actor="system",
        cycle_id=cycle["id"],
        action="attempt_dispatched",
        detail={"sequence_no": sequence_no, "attempt_intent_id": intent_id},
    )
    return TickResult(
        result.notified, result.notify_denied, result.dispatched + 1, result.attempt_denied
    )


@dataclass(frozen=True)
class DrainResult:
    delivered: int = 0
    retried: int = 0
    rail_denied: int = 0


def drain_outbox(
    conn: Conn, *, now: datetime, simulator: SimulatorClient, limit: int = 200
) -> DrainResult:
    result = DrainResult()
    rows = repo.fetch_pending_outbox(conn, now=now, limit=limit)
    for row in rows:
        payload = row["payload"]
        try:
            outcome = simulator.execute(
                cycle_id=payload["cycle_id"],
                sequence_no=payload["sequence_no"],
                idempotency_key=payload["idempotency_key"],
                mandate_id=payload["mandate_id"],
                payer_id=payload["payer_id"],
                amount=payload["amount"],
                scheduled_for=datetime.fromisoformat(payload["scheduled_for"]),
            )
        except RailDenied as exc:
            result = _handle_rail_denied(conn, row, payload, exc, now, result)
            conn.commit()
            continue
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            repo.bump_outbox_attempt(
                conn,
                row["id"],
                next_attempt_at=now + timedelta(seconds=OUTBOX_RETRY_BACKOFF_SECONDS),
            )
            repo.insert_audit(
                conn,
                actor="system",
                cycle_id=payload["cycle_id"],
                action="outbox_retry",
                detail={"error": str(exc)},
            )
            result = DrainResult(result.delivered, result.retried + 1, result.rail_denied)
            conn.commit()
            continue

        result = _handle_delivered(conn, row, payload, outcome, now, result)
        conn.commit()
    return result


def _handle_rail_denied(
    conn: Conn,
    row: dict[str, Any],
    payload: dict[str, Any],
    exc: RailDenied,
    now: datetime,
    result: DrainResult,
) -> DrainResult:
    # Our own scheduling disagreed with the independently-enforcing rail
    # (docs §H.2) — a bug signal, not a transient failure. Don't retry;
    # surface it loudly.
    repo.update_attempt_outcome(
        conn,
        payload["attempt_intent_id"],
        outcome="blocked",
        raw_reason=f"rail_denied:{exc.denial_reason}",
        executed_at=now,
    )
    repo.mark_outbox_delivered(conn, row["id"], delivered_at=now)
    repo.mark_plan_step(
        conn,
        payload["plan_step_id"],
        status="cancelled",
        cancelled_reason=f"rail_denied:{exc.denial_reason}",
    )
    repo.insert_audit(
        conn,
        actor="system",
        cycle_id=payload["cycle_id"],
        action="rail_denied_unexpected",
        detail={"reason": exc.denial_reason, "outbox_id": row["id"]},
    )
    return DrainResult(result.delivered, result.retried, result.rail_denied + 1)


def _handle_delivered(
    conn: Conn,
    row: dict[str, Any],
    payload: dict[str, Any],
    outcome: Any,
    now: datetime,
    result: DrainResult,
) -> DrainResult:
    repo.update_attempt_outcome(
        conn,
        payload["attempt_intent_id"],
        outcome=outcome.outcome,
        raw_reason=outcome.raw_reason,
        executed_at=now,
    )
    repo.mark_outbox_delivered(conn, row["id"], delivered_at=now)
    repo.mark_plan_step(conn, payload["plan_step_id"], status="done")

    cycle_id = payload["cycle_id"]

    # Chaos guard (Phase 7): this attempt was reserved and dispatched while
    # the cycle was EXECUTING, but by the time the rail actually answers, a
    # mandate.revoked/notification.opted_out event may have already
    # resolved the cycle to ABANDONED (ingest._abandon_in_flight_cycles
    # only cancels *pending* steps — a *dispatched* one is already in
    # flight at the rail and can't be recalled). Blindly doing
    # `transition(CaseState.EXECUTING, ...)` here would silently resurrect
    # an already-terminal cycle (e.g. flip a correctly-ABANDONED cycle back
    # to RECOVERED on a late "success"). The rail outcome is still recorded
    # above — that's real and honest — but the cycle's own FSM state is
    # only touched when it's actually still EXECUTING.
    current_cycle = repo.get_cycle(conn, cycle_id)
    assert current_cycle is not None
    if CaseState(current_cycle["state"]) != CaseState.EXECUTING:
        repo.insert_audit(
            conn,
            actor="system",
            cycle_id=cycle_id,
            action="attempt_outcome_after_cycle_resolved",
            detail={
                "outcome": outcome.outcome,
                "raw_reason": outcome.raw_reason,
                "cycle_state": current_cycle["state"],
            },
        )
        return DrainResult(result.delivered + 1, result.retried, result.rail_denied)

    attempts_used = repo.count_attempts(conn, cycle_id)
    if outcome.outcome == "success":
        new_state = transition(CaseState.EXECUTING, Event.ATTEMPT_SUCCEEDED)
        repo.update_cycle_state(
            conn,
            cycle_id,
            state=new_state.value,
            attempts_used=attempts_used,
            recovered_amount=payload["amount"],
            closed_at=now,
        )
        repo.cancel_pending_steps_for_cycle(conn, cycle_id, reason="recovered")
        repo.insert_audit(
            conn,
            actor="system",
            cycle_id=cycle_id,
            action="attempt_succeeded",
            detail={"sequence_no": payload["sequence_no"]},
        )
    elif attempts_used >= MAX_ATTEMPTS:
        new_state = transition(CaseState.EXECUTING, Event.BUDGET_EXHAUSTED)
        repo.update_cycle_state(
            conn, cycle_id, state=new_state.value, attempts_used=attempts_used, closed_at=now
        )
        repo.cancel_pending_steps_for_cycle(conn, cycle_id, reason="budget_exhausted")
        repo.insert_audit(
            conn, actor="system", cycle_id=cycle_id, action="budget_exhausted", detail={}
        )
    else:
        new_state = transition(CaseState.EXECUTING, Event.ATTEMPT_FAILED_BUDGET_LEFT)
        repo.update_cycle_state(
            conn, cycle_id, state=new_state.value, attempts_used=attempts_used
        )
        repo.insert_audit(
            conn,
            actor="system",
            cycle_id=cycle_id,
            action="attempt_failed",
            detail={"outcome": outcome.outcome, "raw_reason": outcome.raw_reason},
        )
    return DrainResult(result.delivered + 1, result.retried, result.rail_denied)
