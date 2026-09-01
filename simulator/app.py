"""Simulator service — the independently-enforcing mandate rail (docs §H).

Runs as a standalone FastAPI app so a bug in the caller (our own worker)
cannot forge success on a fifth attempt or an out-of-window debit (docs
§H.2: "if the simulator lived inside the app it would share the app's
bugs"). State lives in this service's own SQLite store (simulator/store.py),
never shared with the main app's Postgres schema.
"""
from __future__ import annotations

import hashlib
import os
import random
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from simulator.chaos import ChaosConfig, roll_5xx, roll_timeout
from simulator.decline import decide_outcome
from simulator.store import AttemptRecord, SequenceCapViolation, Store

# Non-peak execution window, UTC hour-of-day: 00:00-05:59 and 22:00-23:59.
# Deliberately NOT imported from backend/app/domain/policy.py's identical
# _PERMITTED_WINDOW_HOURS assumption — the whole point of a separate
# simulator (docs §H.2) is that its enforcement is independently coded, so a
# bug in one doesn't silently pass the other. The *values* are kept in sync
# by hand because they represent the same real-world NPCI assumption; if you
# change one, change both and say so in the commit.
PERMITTED_WINDOW_HOURS: tuple[range, ...] = (range(0, 6), range(22, 24))


def _in_permitted_window(hour: int) -> bool:
    return any(hour in window for window in PERMITTED_WINDOW_HOURS)

MAX_SEQUENCE_NO = 4

Outcome = Literal["success", "failure", "unknown"]


class ExecuteRequest(BaseModel):
    cycle_id: str
    sequence_no: int = Field(ge=1, le=MAX_SEQUENCE_NO)
    idempotency_key: str
    mandate_id: str
    payer_id: str | None = None
    amount: float = Field(gt=0)
    scheduled_for: datetime
    issuer_code: str | None = None
    chronic_fail_propensity: float | None = Field(default=None, ge=0.0, le=1.0)
    # Timing/balance-cycle context (docs §J.2, simulator/decline.py) — see
    # this module's docstring on _world_seed_key for why these matter.
    # Optional, defaulting to None: a caller with no payer context (e.g. an
    # older/lighter integration) gets decide_outcome's documented flat
    # fallback rather than an error.
    mean_balance: float | None = None
    balance_volatility: float | None = Field(default=None, gt=0.0)
    credit_day: int | None = Field(default=None, ge=1, le=28)


class ExecuteResponse(BaseModel):
    outcome: Outcome
    raw_reason: str | None = None
    idempotent_replay: bool = False


def _deterministic_rng(seed_key: str) -> random.Random:
    digest = hashlib.sha256(seed_key.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _world_seed_key(req: ExecuteRequest) -> str:
    """What actually happens on a given attempt is a fact about the
    simulated world — this payer, this exact moment, this attempt number —
    not an artifact of which internal cycle_id/idempotency_key our own
    bookkeeping happened to assign it. Keying the outcome draw on
    (payer_id, scheduled_for, sequence_no) instead of idempotency_key means
    two different callers asking about the same real payer at the same
    real moment — e.g. two policies in the Phase 8 paired benchmark, each
    with their own cycle_id/idempotency_key for the same underlying payer —
    see the identical underlying draw: a shared realised world, not
    independent noise per policy. Falls back to idempotency_key when
    payer_id is absent, to preserve prior behaviour exactly rather than
    risk multiple distinct callers colliding on a None-keyed seed. Safe
    either way for idempotent replay itself: a repeated idempotency_key is
    already served from the store before this is ever consulted (see
    `execute` below) — this only decides the *first* draw for a given key.
    """
    if req.payer_id:
        return f"{req.payer_id}:{req.scheduled_for.isoformat()}:{req.sequence_no}"
    return req.idempotency_key


def _to_record(req: ExecuteRequest, outcome: Outcome, raw_reason: str | None) -> AttemptRecord:
    return AttemptRecord(
        cycle_id=req.cycle_id,
        sequence_no=req.sequence_no,
        idempotency_key=req.idempotency_key,
        mandate_id=req.mandate_id,
        payer_id=req.payer_id,
        amount=req.amount,
        scheduled_for=req.scheduled_for.isoformat(),
        outcome=outcome,
        raw_reason=raw_reason,
        executed_at=datetime.now(UTC).isoformat(),
    )


def create_app(*, db_path: str = ":memory:", chaos: ChaosConfig | None = None) -> FastAPI:
    store = Store(db_path)
    state = {"chaos": chaos or ChaosConfig()}
    app = FastAPI(title="MRE Simulator")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/admin/chaos")
    def get_chaos() -> ChaosConfig:
        return state["chaos"]

    @app.post("/admin/chaos")
    def set_chaos(cfg: ChaosConfig) -> ChaosConfig:
        state["chaos"] = cfg
        return cfg

    @app.post("/admin/reset")
    def reset() -> dict[str, str]:
        store.reset()
        return {"status": "reset"}

    @app.post("/execute")
    def execute(req: ExecuteRequest) -> ExecuteResponse:
        existing = store.get_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            outcome: Outcome = existing.outcome  # type: ignore[assignment]
            return ExecuteResponse(
                outcome=outcome, raw_reason=existing.raw_reason, idempotent_replay=True
            )

        if not _in_permitted_window(req.scheduled_for.hour):
            raise HTTPException(
                status_code=409, detail={"denial_reason": "OUTSIDE_EXECUTION_WINDOW"}
            )

        rng = _deterministic_rng(_world_seed_key(req))
        chaos_cfg = state["chaos"]

        if roll_5xx(chaos_cfg, rng):
            # Deliberately not recorded: docs §H.3 — the attempt sequence
            # number must stay reserved by the *caller's* outbox, not
            # consumed here, so redelivery is what retries it.
            raise HTTPException(status_code=503, detail="simulated rail 5xx (chaos)")

        if roll_timeout(chaos_cfg, rng):
            # docs §M.1: never retry an ambiguous debit. This DOES consume
            # the attempt slot — the rail accepted the request, it's the
            # response that's missing.
            _persist(store, req, "unknown", None)
            return ExecuteResponse(outcome="unknown", raw_reason=None)

        result_outcome, raw_reason = decide_outcome(
            rng,
            issuer_code=req.issuer_code,
            chronic_fail_propensity=req.chronic_fail_propensity,
            mean_balance=req.mean_balance,
            balance_volatility=req.balance_volatility,
            day_of_month=req.scheduled_for.day,
            credit_day=req.credit_day,
            amount=req.amount,
        )
        outcome = "success" if result_outcome == "success" else "failure"
        _persist(store, req, outcome, raw_reason)
        return ExecuteResponse(outcome=outcome, raw_reason=raw_reason)

    return app


def _persist(store: Store, req: ExecuteRequest, outcome: Outcome, raw_reason: str | None) -> None:
    try:
        store.insert_attempt(_to_record(req, outcome, raw_reason))
    except SequenceCapViolation as exc:
        raise HTTPException(
            status_code=409, detail={"denial_reason": "NPCI_ATTEMPT_CAP_EXCEEDED"}
        ) from exc


# Defaults to in-memory so merely importing this module (e.g. under pytest
# collection) never scatters a stray db file. Real runs set SIMULATOR_DB_PATH
# explicitly (wired into docker-compose in a later phase).
app = create_app(db_path=os.environ.get("SIMULATOR_DB_PATH", ":memory:"))
