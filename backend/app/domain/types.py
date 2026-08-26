"""Shared types for the domain core. Pure: no I/O, no clock, no network."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Cause(StrEnum):
    """Canonical decline-reason taxonomy (docs §J.3)."""

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_PAUSED = "MANDATE_PAUSED"
    ACCOUNT_FROZEN = "ACCOUNT_FROZEN"
    ACCOUNT_CLOSED = "ACCOUNT_CLOSED"
    AFA_REQUIRED = "AFA_REQUIRED"
    ISSUER_TECH_DECLINE = "ISSUER_TECH_DECLINE"
    PSP_TECH_DECLINE = "PSP_TECH_DECLINE"
    TIMEOUT = "TIMEOUT"
    INVALID_MANDATE_STATE = "INVALID_MANDATE_STATE"
    RISK_DECLINE = "RISK_DECLINE"
    UNKNOWN = "UNKNOWN"


class RetryDisposition(StrEnum):
    RETRY_TIMING_SENSITIVE = "RETRY_TIMING_SENSITIVE"
    RETRY_TRANSIENT = "RETRY_TRANSIENT"
    NO_RETRY = "NO_RETRY"
    NEEDS_HUMAN = "NEEDS_HUMAN"


# Every cause must map to exactly one disposition (docs §J.3). Enforced by
# CAUSE_DISPOSITION covering all 13 enum members — see test_types.py.
CAUSE_DISPOSITION: dict[Cause, RetryDisposition] = {
    Cause.INSUFFICIENT_FUNDS: RetryDisposition.RETRY_TIMING_SENSITIVE,
    Cause.LIMIT_EXCEEDED: RetryDisposition.RETRY_TIMING_SENSITIVE,
    Cause.MANDATE_REVOKED: RetryDisposition.NO_RETRY,
    Cause.MANDATE_PAUSED: RetryDisposition.NO_RETRY,
    Cause.ACCOUNT_FROZEN: RetryDisposition.NEEDS_HUMAN,
    Cause.ACCOUNT_CLOSED: RetryDisposition.NO_RETRY,
    Cause.AFA_REQUIRED: RetryDisposition.NEEDS_HUMAN,
    Cause.ISSUER_TECH_DECLINE: RetryDisposition.RETRY_TRANSIENT,
    Cause.PSP_TECH_DECLINE: RetryDisposition.RETRY_TRANSIENT,
    Cause.TIMEOUT: RetryDisposition.RETRY_TRANSIENT,
    Cause.INVALID_MANDATE_STATE: RetryDisposition.NEEDS_HUMAN,
    Cause.RISK_DECLINE: RetryDisposition.NEEDS_HUMAN,
    Cause.UNKNOWN: RetryDisposition.NEEDS_HUMAN,
}


class ActionType(StrEnum):
    IDLE = "IDLE"
    NOTIFY = "NOTIFY"
    ATTEMPT = "ATTEMPT"
    STOP_AND_ESCALATE = "STOP_AND_ESCALATE"


class CaseState(StrEnum):
    """Recovery-case state machine (docs §P.3)."""

    DUE = "DUE"
    DIAGNOSING = "DIAGNOSING"
    PLANNING = "PLANNING"
    SCHEDULED = "SCHEDULED"
    EXECUTING = "EXECUTING"
    ESCALATING = "ESCALATING"
    AWAITING_MANUAL = "AWAITING_MANUAL"
    RECOVERED = "RECOVERED"
    ABANDONED = "ABANDONED"


TERMINAL_STATES: frozenset[CaseState] = frozenset({CaseState.RECOVERED, CaseState.ABANDONED})

MAX_ATTEMPTS = 4
NOTICE_MIN_HOURS = 24
NOTICE_MAX_AGE_DAYS = 7

AFA_THRESHOLD_DEFAULT = 15_000
AFA_THRESHOLD_INSURANCE_MF_CREDITCARD = 100_000


class DenyReason(StrEnum):
    ATTEMPT_BUDGET_EXHAUSTED = "ATTEMPT_BUDGET_EXHAUSTED"
    OUTSIDE_EXECUTION_WINDOW = "OUTSIDE_EXECUTION_WINDOW"
    RBI_NOTICE_NOT_SATISFIED = "RBI_NOTICE_NOT_SATISFIED"
    MANDATE_NOT_ACTIVE = "MANDATE_NOT_ACTIVE"
    OPTED_OUT = "OPTED_OUT"
    CONTACT_CAP_EXCEEDED = "CONTACT_CAP_EXCEEDED"
    OUTSIDE_QUIET_HOURS = "OUTSIDE_QUIET_HOURS"
    AFA_REQUIRED_NOT_SATISFIED = "AFA_REQUIRED_NOT_SATISFIED"
    MERCHANT_KILL_SWITCH = "MERCHANT_KILL_SWITCH"
    GLOBAL_KILL_SWITCH = "GLOBAL_KILL_SWITCH"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason_code: DenyReason | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.reason_code is not None:
            raise ValueError("an Allow verdict must not carry a reason_code")
        if not self.allowed and self.reason_code is None:
            raise ValueError("a Deny verdict must carry a reason_code")

    @staticmethod
    def allow() -> Verdict:
        return Verdict(allowed=True)

    @staticmethod
    def deny(reason_code: DenyReason) -> Verdict:
        return Verdict(allowed=False, reason_code=reason_code)


@dataclass(frozen=True)
class NoticeRecord:
    sent_at: datetime
    covers_debit_at: datetime


@dataclass(frozen=True)
class CaseSnapshot:
    """Everything authorize() needs to decide, as of a given clock tick."""

    state: CaseState
    attempts_used: int
    mandate_active: bool
    opted_out: bool
    amount: int
    afa_threshold: int
    afa_satisfied: bool
    notices: tuple[NoticeRecord, ...]
    contact_count_today: int
    contact_cap: int
    quiet_hours_active: bool
    merchant_kill_switch: bool
    global_kill_switch: bool


@dataclass(frozen=True)
class MandateSnapshot:
    """Pure feature-assembly input, shared by training and inference."""

    cause: Cause
    disposition: RetryDisposition
    attempt_sequence_no: int
    hours_since_last_failure: float
    day_of_month: int
    days_to_credit_day: int
    slot_of_day: int
    day_of_week: int
    amount: int
    amount_over_historical_mean: float
    mandate_age_days: int
    payer_prior_success_rate: float
    consecutive_prior_failures: int
    issuer_historical_success_rate: float
    issuer_downtime_active: bool
    rail: str
    segment_proxy: str
    notices_sent_this_cycle: int
    days_since_last_notice: float | None
