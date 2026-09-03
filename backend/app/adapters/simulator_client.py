"""HTTP adapter to the simulator service — the only module that knows its
wire format. Everything else in the app talks to "the rail" through this
interface, so a future RazorpayRailClient (only ever written once/if real
UPI Autopay is actually enabled and testable — see
docs/RAZORPAY_TESTMODE_FINDINGS.md 7) is a drop-in, never a rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

from app.config import get_settings

Outcome = Literal["success", "failure", "unknown"]


@dataclass(frozen=True)
class ExecuteResult:
    outcome: Outcome
    raw_reason: str | None
    idempotent_replay: bool


class RailDenied(Exception):
    """The rail independently refused the action (docs H.2) — e.g. the
    NPCI attempt cap or the execution window. Never retry blindly on this;
    it means our own scheduling was wrong, not that the call failed."""

    def __init__(self, denial_reason: str) -> None:
        self.denial_reason = denial_reason
        super().__init__(denial_reason)


class SimulatorClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self._base_url = base_url or get_settings().simulator_url
        self._timeout = timeout

    def execute(
        self,
        *,
        cycle_id: str,
        sequence_no: int,
        idempotency_key: str,
        mandate_id: str,
        payer_id: str | None,
        amount: float,
        scheduled_for: datetime,
        issuer_code: str | None = None,
        chronic_fail_propensity: float | None = None,
        mean_balance: float | None = None,
        balance_volatility: float | None = None,
        credit_day: int | None = None,
    ) -> ExecuteResult:
        payload: dict[str, Any] = {
            "cycle_id": cycle_id,
            "sequence_no": sequence_no,
            "idempotency_key": idempotency_key,
            "mandate_id": mandate_id,
            "payer_id": payer_id,
            "amount": amount,
            "scheduled_for": scheduled_for.isoformat(),
            "issuer_code": issuer_code,
            "chronic_fail_propensity": chronic_fail_propensity,
            "mean_balance": mean_balance,
            "balance_volatility": balance_volatility,
            "credit_day": credit_day,
        }
        resp = httpx.post(f"{self._base_url}/execute", json=payload, timeout=self._timeout)
        if resp.status_code == 409:
            raise RailDenied(resp.json()["detail"]["denial_reason"])
        resp.raise_for_status()
        body = resp.json()
        outcome: Outcome = body["outcome"]
        return ExecuteResult(
            outcome=outcome,
            raw_reason=body.get("raw_reason"),
            idempotent_replay=body.get("idempotent_replay", False),
        )
