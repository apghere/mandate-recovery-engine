"""Phase 9 dashboard read/admin surface (app/api/dashboard.py), exercised
over real HTTP against real Postgres — same pattern as test_api.py.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from app.api.app import app
from app.db import Conn
from app.ingest import (
    CycleDueEvent,
    DebitOutcomeEvent,
    ingest_cycle_due,
    ingest_debit_failed,
)
from fastapi.testclient import TestClient

client = TestClient(app)

DUE_DATE = date(2026, 9, 1)
DUE_AT = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)


def _seed_a_case(db: Conn, cycle_id: str = "CYC-DASH") -> None:
    ingest_cycle_due(
        db,
        CycleDueEvent(
            external_id=f"ext:{cycle_id}:due",
            mandate_id="M-DASH",
            cycle_id=cycle_id,
            merchant_id="MERCH-DASH",
            payer_id="PAYER-DASH",
            rail="upi_autopay",
            issuer_code="ISS01",
            amount=500.0,
            due_date=DUE_DATE,
            occurred_at=DUE_AT,
        ),
    )
    ingest_debit_failed(
        db,
        DebitOutcomeEvent(
            external_id=f"ext:{cycle_id}:fail1",
            mandate_id="M-DASH",
            cycle_id=cycle_id,
            occurred_at=DUE_AT,
            amount=500.0,
            raw_reason="INSUFFICIENT FUNDS",
        ),
    )


def test_list_cases_returns_the_seeded_case(db: Conn) -> None:
    _seed_a_case(db)
    resp = client.get("/cases")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    ids = [c["id"] for c in body["cases"]]
    assert "CYC-DASH" in ids


def test_list_cases_filters_by_state(db: Conn) -> None:
    _seed_a_case(db)
    resp = client.get("/cases", params={"state": "SCHEDULED"})
    assert resp.status_code == 200
    body = resp.json()
    assert all(c["state"] == "SCHEDULED" for c in body["cases"])


def test_get_case_detail_includes_plan_and_counterfactual(db: Conn) -> None:
    _seed_a_case(db)
    resp = client.get("/cases/CYC-DASH")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cycle"]["id"] == "CYC-DASH"
    assert body["plan"] is not None
    assert len(body["plan_steps"]) == 6  # 3 notify + 3 attempt, P0 fixed default
    assert len(body["fixed_schedule_counterfactual"]) == 6
    assert len(body["attempt_intents"]) == 1  # seq 1, the external failure
    assert any(entry["action"] == "cause_normalized" for entry in body["audit_trail"])


def test_get_case_detail_404_for_unknown_cycle(db: Conn) -> None:
    resp = client.get("/cases/CYC-DOES-NOT-EXIST")
    assert resp.status_code == 404


def test_metrics_is_prometheus_text_format(db: Conn) -> None:
    _seed_a_case(db)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert "# TYPE mre_cases_total gauge" in text
    assert 'mre_cases_total{state="SCHEDULED"}' in text


def test_audit_overview_reports_a_valid_chain(db: Conn) -> None:
    _seed_a_case(db)
    resp = client.get("/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chain_valid"] is True
    assert body["chain_broken_at_id"] is None
    assert len(body["recent_entries"]) >= 1


def test_kill_switch_round_trip(db: Conn) -> None:
    empty = client.get("/admin/kill-switches")
    assert empty.json()["active"] == []

    resp = client.post(
        "/admin/kill-switches",
        json={"scope": "global", "active": True, "set_by": "test-operator"},
    )
    assert resp.status_code == 200
    assert resp.json()["kill_switch"]["active"] is True

    listed = client.get("/admin/kill-switches")
    scopes = [row["scope"] for row in listed.json()["active"]]
    assert "global" in scopes

    audit = client.get("/audit")
    actions = [e["action"] for e in audit.json()["recent_entries"]]
    assert "kill_switch_toggled" in actions

    off = client.post(
        "/admin/kill-switches",
        json={"scope": "global", "active": False, "set_by": "test-operator"},
    )
    assert off.json()["kill_switch"]["active"] is False


def test_kill_switch_rejects_invalid_scope(db: Conn) -> None:
    resp = client.post(
        "/admin/kill-switches",
        json={"scope": "not-a-valid-scope", "active": True, "set_by": "test-operator"},
    )
    assert resp.status_code == 422


def test_global_kill_switch_actually_blocks_the_worker(db: Conn) -> None:
    """Not just a flag that gets stored — proves worker.py's
    _build_snapshot actually reads it and authorize() actually denies."""
    from app.workflows.worker import process_due_plan_steps

    _seed_a_case(db, cycle_id="CYC-DASH-KILL")
    client.post(
        "/admin/kill-switches",
        json={"scope": "global", "active": True, "set_by": "test-operator"},
    )
    try:
        first_step = db.execute(
            "SELECT ps.* FROM plan_steps ps JOIN plans p ON p.id = ps.plan_id "
            "WHERE p.cycle_id = %s ORDER BY ps.scheduled_for LIMIT 1",
            ("CYC-DASH-KILL",),
        ).fetchone()
        assert first_step is not None
        process_due_plan_steps(db, now=first_step["scheduled_for"])
        step_after = db.execute(
            "SELECT * FROM plan_steps WHERE id = %s", (first_step["id"],)
        ).fetchone()
        assert step_after is not None
        assert step_after["cancelled_reason"] == "GLOBAL_KILL_SWITCH"
    finally:
        client.post(
            "/admin/kill-switches",
            json={"scope": "global", "active": False, "set_by": "test-operator"},
        )
