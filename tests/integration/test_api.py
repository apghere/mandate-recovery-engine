"""API layer test: same real Postgres, exercised over HTTP via TestClient
rather than by calling app.ingest functions directly (tests/integration/
test_worker_pipeline.py covers those) — this is what actually proves the
thin api/app.py wiring (request validation, connection lifecycle) works,
not just the service layer underneath it.
"""
from __future__ import annotations

from app.api.app import app
from app.db import Conn
from fastapi.testclient import TestClient

client = TestClient(app)


def _event_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "external_id": "ext:api-test:due",
        "type": "mandate.cycle.due",
        "mandate_id": "M-API",
        "cycle_id": "CYC-API",
        "occurred_at": "2026-09-01T02:00:00Z",
        "payload": {
            "merchant_id": "MERCH1",
            "payer_id": "PAYER1",
            "rail": "upi_autopay",
            "issuer_code": "ISS01",
            "amount": 500.0,
            "due_date": "2026-09-01",
        },
    }
    payload.update(overrides)
    return payload


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200


def test_post_cycle_due_event_creates_mandate_and_cycle(db: Conn) -> None:
    resp = client.post("/events", json=_event_payload())
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "duplicate": False}

    cycle = db.execute("SELECT * FROM cycles WHERE id = %s", ("CYC-API",)).fetchone()
    assert cycle is not None
    assert cycle["state"] == "DUE"


def test_duplicate_post_is_idempotent(db: Conn) -> None:
    first = client.post("/events", json=_event_payload())
    second = client.post("/events", json=_event_payload())
    assert first.json()["accepted"] is True
    assert second.json() == {"accepted": False, "duplicate": True}


def test_malformed_payload_returns_422(db: Conn) -> None:
    resp = client.post(
        "/events", json=_event_payload(payload={"merchant_id": "MERCH1"})
    )
    assert resp.status_code == 422
