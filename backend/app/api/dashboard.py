"""Read/admin surface for the dashboard (Phase 9, docs FR-12): case
detail with the plan timeline and the fixed-schedule counterfactual,
Prometheus /metrics, the audit ledger view with chain-validity, and the
kill switch. Deliberately separate from api/app.py's ingestion endpoint —
that file is the webhook surface; this one is read-mostly plus a single,
explicitly-authorised admin write (docs §I.10: "operator overrides are
separate actions that are themselves authorised").

Every response here is built from real repo.py reads over the same
Postgres tables the worker/ingest pipeline writes — no separate
materialized view, no caching layer, so the dashboard can never show
something the system doesn't actually believe.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app import repo
from app.db import get_connection
from app.policies.fixed import compute_fixed_schedule

router = APIRouter()


@router.get("/cases")
def list_cases(
    state: str | None = None, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    with get_connection() as conn:
        cases = repo.list_cycles(conn, state=state, limit=limit, offset=offset)
        total = repo.count_cycles(conn, state=state)
    return {"total": total, "limit": limit, "offset": offset, "cases": cases}


@router.get("/cases/{cycle_id}")
def get_case(cycle_id: str) -> dict[str, Any]:
    with get_connection() as conn:
        cycle = repo.get_cycle(conn, cycle_id)
        if cycle is None:
            raise HTTPException(status_code=404, detail=f"no such case: {cycle_id}")
        mandate = repo.get_mandate(conn, cycle["mandate_id"])
        assert mandate is not None

        plan = repo.latest_plan_for_cycle(conn, cycle_id)
        plan_steps = repo.plan_steps_for_plan(conn, plan["id"]) if plan is not None else []
        attempts = repo.attempt_intents_for_cycle(conn, cycle_id)
        notifications = repo.notifications_for_cycle(conn, cycle_id)
        decisions = repo.decisions_for_cycle(conn, cycle_id)
        audit = repo.audit_for_cycle(conn, cycle_id)

    # The side-by-side counterfactual (docs FR-12 / Day 6 priority #1): what
    # P0's fixed D+1/D+3/D+7 schedule would have looked like for this same
    # cycle, computed live from the pure policy function — never persisted,
    # so it can't drift from what compute_fixed_schedule actually does.
    counterfactual = [asdict(step) for step in compute_fixed_schedule(cycle["due_date"])]

    return {
        "cycle": cycle,
        "mandate": mandate,
        "plan": plan,
        "plan_steps": plan_steps,
        "fixed_schedule_counterfactual": counterfactual,
        "attempt_intents": attempts,
        "notifications": notifications,
        "decisions": decisions,
        "audit_trail": audit,
    }


def _prometheus_escape(label_value: str) -> str:
    return label_value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@router.get("/metrics")
def metrics() -> Response:
    """Prometheus text exposition format (docs §I.11)."""
    with get_connection() as conn:
        snap = repo.metrics_snapshot(conn)

    lines: list[str] = []

    lines.append("# HELP mre_cases_total Cases by current FSM state.")
    lines.append("# TYPE mre_cases_total gauge")
    for state, n in snap["cases_by_state"].items():
        lines.append(f'mre_cases_total{{state="{_prometheus_escape(state)}"}} {n}')

    lines.append("# HELP mre_attempts_consumed_total Attempt intents executed.")
    lines.append("# TYPE mre_attempts_consumed_total counter")
    lines.append(f"mre_attempts_consumed_total {snap['attempts_consumed']}")

    lines.append("# HELP mre_policy_denials_total Policy-gate denials by reason code.")
    lines.append("# TYPE mre_policy_denials_total counter")
    for reason, n in snap["policy_denials_by_reason_code"].items():
        label = _prometheus_escape(reason or "unknown")
        lines.append(f'mre_policy_denials_total{{reason_code="{label}"}} {n}')

    lines.append("# HELP mre_cause_normalization_total Decline causes by normalization source.")
    lines.append("# TYPE mre_cause_normalization_total counter")
    for source, n in snap["cause_normalization_source_counts"].items():
        label = _prometheus_escape(source or "unknown")
        lines.append(f'mre_cause_normalization_total{{source="{label}"}} {n}')

    lines.append("# HELP mre_notifications_total Notices sent by how the body was generated.")
    lines.append("# TYPE mre_notifications_total counter")
    for gen_by, n in snap["notification_generated_by_counts"].items():
        label = _prometheus_escape(gen_by or "unknown")
        lines.append(f'mre_notifications_total{{generated_by="{label}"}} {n}')

    lines.append(
        "# HELP mre_notification_validator_repaired_total "
        "Notices whose first LLM draft failed validation and needed a repair pass."
    )
    lines.append("# TYPE mre_notification_validator_repaired_total counter")
    lines.append(f"mre_notification_validator_repaired_total {snap['notification_repaired_count']}")

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/audit")
def audit_overview(limit: int = 200) -> dict[str, Any]:
    limit = max(1, min(limit, 1000))
    with get_connection() as conn:
        valid, broken_at_id = repo.verify_audit_chain(conn)
        entries = repo.recent_audit(conn, limit=limit)
        active_kill_switches = repo.list_kill_switches(conn)
    return {
        "chain_valid": valid,
        "chain_broken_at_id": broken_at_id,
        "active_kill_switches": active_kill_switches,
        "recent_entries": entries,
    }


class KillSwitchRequest(BaseModel):
    scope: str  # "global" or "merchant:<merchant_id>" -- validated below
    active: bool
    set_by: str


@router.get("/admin/kill-switches")
def list_kill_switches() -> dict[str, Any]:
    with get_connection() as conn:
        return {"active": repo.list_kill_switches(conn)}


@router.post("/admin/kill-switches")
def set_kill_switch(req: KillSwitchRequest) -> dict[str, Any]:
    if req.scope != "global" and not req.scope.startswith("merchant:"):
        raise HTTPException(
            status_code=422, detail="scope must be 'global' or 'merchant:<merchant_id>'"
        )
    with get_connection() as conn:
        repo.set_kill_switch(conn, req.scope, active=req.active, set_by=req.set_by)
        # This IS the "operator overrides are separate actions that are
        # themselves authorised" record docs §I.10 requires — an explicit
        # audit entry, not folded silently into the toggle itself.
        repo.insert_audit(
            conn,
            actor=f"operator:{req.set_by}",
            cycle_id=None,
            action="kill_switch_toggled",
            detail={"scope": req.scope, "active": req.active},
        )
        conn.commit()
        current = repo.get_kill_switch(conn, req.scope)
    return {"kill_switch": current}
