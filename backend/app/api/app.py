"""Thin HTTP layer over app.ingest (FR-1, docs §H.1's `/events`).

Deliberately thin: this module owns request validation, webhook signature
verification, and connection lifecycle only. All ingestion logic lives in
app.ingest so the replay driver (scripts/replay_fixed.py) can call the
same functions directly without paying an HTTP round trip per mandate,
while still exercising the identical idempotency/transaction behaviour a
real webhook would hit.

Signature verification (Phase 7, docs §H.1): HMAC-SHA256 over the raw
request body, same mechanism scripts/webhook_capture.py's spike tool
already proved against real Razorpay deliveries
(docs/RAZORPAY_TESTMODE_FINDINGS.md §6-7), `hmac.compare_digest` to avoid a
timing side-channel. When `RAZORPAY_WEBHOOK_SECRET` isn't configured
(local dev, tests, replay scripts talking to app.ingest directly) this
degrades to accepting unsigned requests — documented, not silent: it's the
only way `make replay-fixed`/the integration test suite can keep hitting
this endpoint without a secret ever being provisioned for them, and it's a
strictly *more* permissive default than refusing to boot, which would be
the wrong failure mode for a hackathon judge's local `make up`.

One connection per request for now — a pool is a later optimization, not a
Day-2 correctness concern.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from app.api.dashboard import router as dashboard_router
from app.config import get_settings
from app.db import get_connection
from app.ingest import (
    CycleDueEvent,
    DebitOutcomeEvent,
    IngestResult,
    MandateLifecycleEvent,
    UnknownCycleError,
    ingest_cycle_due,
    ingest_debit_failed,
    ingest_debit_succeeded,
    ingest_mandate_revoked,
    ingest_notification_opted_out,
)

app = FastAPI(title="Mandate Recovery Engine — ingestion API")
app.include_router(dashboard_router)

# Phase 9's dashboard (frontend/*.html — no build step, on purpose: "fresh
# clone -> demo in three commands"). Mounted last so it never shadows the
# /events, /cases, /metrics, /audit, /admin routes above. /reports serves
# evaluation/runner.py's generated BENCHMARK.md/benchmark.json and
# app/ml/calibrate.py's calibration.png for the benchmark screen.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if (_REPO_ROOT / "reports").is_dir():
    app.mount("/reports", StaticFiles(directory=_REPO_ROOT / "reports"), name="reports")
if (_REPO_ROOT / "frontend").is_dir():
    app.mount(
        "/dashboard", StaticFiles(directory=_REPO_ROOT / "frontend", html=True), name="dashboard"
    )


def _verify_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = get_settings().webhook_secret
    if not secret:
        return True  # see module docstring's signature-verification note
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class MandateCycleDuePayload(BaseModel):
    merchant_id: str
    payer_id: str
    rail: str
    issuer_code: str
    amount: float
    due_date: date


class DebitOutcomePayload(BaseModel):
    amount: float
    raw_reason: str | None = None


class EventEnvelope(BaseModel):
    external_id: str
    type: Literal[
        "mandate.cycle.due",
        "debit.succeeded",
        "debit.failed",
        "mandate.revoked",
        "notification.opted_out",
    ]
    mandate_id: str
    cycle_id: str | None = None
    occurred_at: datetime
    payload: dict[str, Any] = {}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events")
async def post_event(request: Request) -> dict[str, bool]:
    raw_body = await request.body()
    if not _verify_signature(raw_body, request.headers.get("x-razorpay-signature")):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    try:
        envelope = EventEnvelope.model_validate_json(raw_body)
        result = _dispatch(envelope)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnknownCycleError as exc:
        # Out-of-order delivery (docs §M.1), not a bug: retryable, not 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"accepted": result.accepted, "duplicate": result.duplicate}


def _dispatch(envelope: EventEnvelope) -> IngestResult:
    with get_connection() as conn:
        if envelope.type == "mandate.cycle.due":
            assert envelope.cycle_id is not None
            p = MandateCycleDuePayload(**envelope.payload)
            return ingest_cycle_due(
                conn,
                CycleDueEvent(
                    external_id=envelope.external_id,
                    mandate_id=envelope.mandate_id,
                    cycle_id=envelope.cycle_id,
                    merchant_id=p.merchant_id,
                    payer_id=p.payer_id,
                    rail=p.rail,
                    issuer_code=p.issuer_code,
                    amount=p.amount,
                    due_date=p.due_date,
                    occurred_at=envelope.occurred_at,
                ),
            )
        if envelope.type in ("mandate.revoked", "notification.opted_out"):
            lifecycle_event = MandateLifecycleEvent(
                external_id=envelope.external_id,
                mandate_id=envelope.mandate_id,
                occurred_at=envelope.occurred_at,
            )
            if envelope.type == "mandate.revoked":
                return ingest_mandate_revoked(conn, lifecycle_event)
            return ingest_notification_opted_out(conn, lifecycle_event)
        assert envelope.cycle_id is not None
        p2 = DebitOutcomePayload(**envelope.payload)
        outcome_event = DebitOutcomeEvent(
            external_id=envelope.external_id,
            mandate_id=envelope.mandate_id,
            cycle_id=envelope.cycle_id,
            occurred_at=envelope.occurred_at,
            amount=p2.amount,
            raw_reason=p2.raw_reason,
        )
        if envelope.type == "debit.succeeded":
            return ingest_debit_succeeded(conn, outcome_event)
        return ingest_debit_failed(conn, outcome_event)
