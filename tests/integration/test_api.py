"""API layer test: same real Postgres, exercised over HTTP via TestClient
rather than by calling app.ingest functions directly (tests/integration/
test_worker_pipeline.py covers those) — this is what actually proves the
thin api/app.py wiring (request validation, webhook signature
verification, connection lifecycle) works, not just the service layer
underneath it.
"""
from __future__ import annotations

import hashlib
import hmac
import json

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


def test_events_accepted_unsigned_when_no_secret_configured(monkeypatch, db: Conn) -> None:
    # The documented dev-mode default (api/app.py's module docstring):
    # local dev, this test suite, and replay scripts never provision a
    # secret, and must not be locked out because of that.
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    resp = client.post("/events", json=_event_payload(external_id="ext:api-test:unsigned"))
    assert resp.status_code == 200


def test_events_rejects_missing_signature_when_secret_configured(monkeypatch, db: Conn) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test-secret")
    resp = client.post("/events", json=_event_payload(external_id="ext:api-test:nosig"))
    assert resp.status_code == 401


def test_events_rejects_wrong_signature_when_secret_configured(monkeypatch, db: Conn) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test-secret")
    resp = client.post(
        "/events",
        json=_event_payload(external_id="ext:api-test:badsig"),
        headers={"x-razorpay-signature": "0" * 64},
    )
    assert resp.status_code == 401


def test_events_accepts_valid_signature(monkeypatch, db: Conn) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test-secret")
    body = json.dumps(_event_payload(external_id="ext:api-test:goodsig")).encode("utf-8")
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/events",
        content=body,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": signature,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "duplicate": False}


def test_mandate_revoked_event_over_http(monkeypatch, db: Conn) -> None:
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    due_resp = client.post(
        "/events",
        json=_event_payload(external_id="ext:api-lifecycle:due", cycle_id="CYC-LIFECYCLE"),
    )
    assert due_resp.status_code == 200

    revoked_resp = client.post(
        "/events",
        json={
            "external_id": "ext:api-lifecycle:revoked",
            "type": "mandate.revoked",
            "mandate_id": "M-API",
            "occurred_at": "2026-09-01T03:00:00Z",
            "payload": {},
        },
    )
    assert revoked_resp.status_code == 200
    assert revoked_resp.json() == {"accepted": True, "duplicate": False}

    mandate = db.execute("SELECT status FROM mandates WHERE id = %s", ("M-API",)).fetchone()
    assert mandate is not None and mandate["status"] == "revoked"


def test_unknown_cycle_returns_409_not_500(monkeypatch, db: Conn) -> None:
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    resp = client.post(
        "/events",
        json=_event_payload(
            external_id="ext:api-ooo:fail1",
            type="debit.failed",
            cycle_id="CYC-NEVER-SEEN",
            payload={"amount": 500.0, "raw_reason": "INSUFFICIENT FUNDS"},
        ),
    )
    assert resp.status_code == 409
