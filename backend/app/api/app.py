"""Thin HTTP layer over app.ingest (FR-1, docs §H.1's `/events`).

Deliberately thin: this module owns request validation and connection
lifecycle only. All ingestion logic lives in app.ingest so the replay
driver (scripts/replay_fixed.py) can call the same functions directly
without paying an HTTP round trip per mandate, while still exercising the
identical idempotency/transaction behaviour a real webhook would hit.

One connection per request for now — a pool is a later optimization, not a
Day-2 correctness concern.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from app.db import get_connection
from app.ingest import (
    CycleDueEvent,
    DebitOutcomeEvent,
    IngestResult,
    ingest_cycle_due,
    ingest_debit_failed,
    ingest_debit_succeeded,
)

app = FastAPI(title="Mandate Recovery Engine — ingestion API")


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
    type: Literal["mandate.cycle.due", "debit.succeeded", "debit.failed"]
    mandate_id: str
    cycle_id: str
    occurred_at: datetime
    payload: dict[str, Any]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events")
def post_event(envelope: EventEnvelope) -> dict[str, bool]:
    try:
        result = _dispatch(envelope)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"accepted": result.accepted, "duplicate": result.duplicate}


def _dispatch(envelope: EventEnvelope) -> IngestResult:
    with get_connection() as conn:
        if envelope.type == "mandate.cycle.due":
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
