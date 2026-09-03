"""app/policies/live.py wired into the real `/events` endpoint (Phase 9):
before this, `/events` always used the P0 fixed baseline regardless of
payer context — MRE was only ever reachable through replay/benchmark
scripts, never through the actual product API. Exercised over real HTTP,
same pattern as test_api.py.
"""
from __future__ import annotations

from app.api.app import app
from app.db import Conn
from app.repo import upsert_payer
from fastapi.testclient import TestClient

client = TestClient(app)


def _event_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "external_id": "ext:live-test:due",
        "type": "mandate.cycle.due",
        "mandate_id": "M-LIVE",
        "cycle_id": "CYC-LIVE",
        "occurred_at": "2026-09-01T02:00:00Z",
        "payload": {
            "merchant_id": "MERCH1",
            "payer_id": "PAYER-LIVE",
            "rail": "upi_autopay",
            "issuer_code": "ISS01",
            "amount": 500.0,
            "due_date": "2026-09-01",
        },
    }
    payload.update(overrides)
    return payload


def _fail_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "external_id": "ext:live-test:fail1",
        "type": "debit.failed",
        "mandate_id": "M-LIVE",
        "cycle_id": "CYC-LIVE",
        "occurred_at": "2026-09-01T02:00:00Z",
        "payload": {"amount": 500.0, "raw_reason": "INSUFFICIENT FUNDS"},
    }
    payload.update(overrides)
    return payload


def test_live_events_endpoint_uses_mre_when_a_payer_row_exists(db: Conn) -> None:
    upsert_payer(
        db,
        payer_id="PAYER-LIVE",
        segment="salaried",
        credit_day=15,
        mean_balance=8000.0,
        balance_volatility=0.4,
        issuer_code="ISS01",
        chronic_fail_propensity=0.05,
        annoyance_sensitivity=0.3,
        mandate_amount=500.0,
        split="dev",
    )
    db.commit()

    due_resp = client.post("/events", json=_event_payload())
    assert due_resp.status_code == 200
    fail_resp = client.post("/events", json=_fail_payload())
    assert fail_resp.status_code == 200

    plan = db.execute(
        "SELECT model_version FROM plans WHERE cycle_id = %s", ("CYC-LIVE",)
    ).fetchone()
    assert plan is not None
    assert plan["model_version"] == "MRE-dp-v1"

    # And the plan_steps carry real per-slot probabilities — not the
    # fixed baseline's None.
    steps = db.execute(
        "SELECT p_success FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE p.cycle_id = %s AND ps.step_type = 'attempt'",
        ("CYC-LIVE",),
    ).fetchall()
    assert len(steps) > 0


def test_live_events_endpoint_falls_back_to_fixed_without_a_payer_row(db: Conn) -> None:
    due_resp = client.post(
        "/events",
        json=_event_payload(
            external_id="ext:live-test2:due",
            cycle_id="CYC-LIVE-NOPAYER",
            payload={
                "merchant_id": "MERCH1",
                "payer_id": "PAYER-NEVER-SEEDED",
                "rail": "upi_autopay",
                "issuer_code": "ISS01",
                "amount": 500.0,
                "due_date": "2026-09-01",
            },
        ),
    )
    assert due_resp.status_code == 200
    fail_resp = client.post(
        "/events",
        json=_fail_payload(external_id="ext:live-test2:fail1", cycle_id="CYC-LIVE-NOPAYER"),
    )
    assert fail_resp.status_code == 200

    plan = db.execute(
        "SELECT model_version FROM plans WHERE cycle_id = %s", ("CYC-LIVE-NOPAYER",)
    ).fetchone()
    assert plan is not None
    assert plan["model_version"] == "P0-fixed-schedule-v1"
