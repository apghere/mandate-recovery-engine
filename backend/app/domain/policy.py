"""The policy engine (docs §I.10). Pure: no I/O, no clock, no network — the
clock is always injected as `now`/`target_time` arguments.

Checks, in order: kill switches, mandate status, opt-out, then action-specific
rules (attempt budget, execution window, notice freshness, AFA threshold for
ATTEMPT; contact cap and quiet hours for NOTIFY).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.types import (
    MAX_ATTEMPTS,
    NOTICE_MAX_AGE_DAYS,
    NOTICE_MIN_HOURS,
    ActionType,
    CaseSnapshot,
    DenyReason,
    NoticeRecord,
    Verdict,
)

# NPCI does not publish exact clock times for "non-peak" UPI Autopay
# execution windows; this is a documented placeholder assumption
# (00:00-06:00 and 22:00-23:59 local), to be validated against real
# merchant/PSP data. See docs/ADR/ for the tracked assumption.
_PERMITTED_WINDOW_HOURS: tuple[range, ...] = (range(0, 6), range(22, 24))


def _in_permitted_window(t: datetime) -> bool:
    return any(t.hour in window for window in _PERMITTED_WINDOW_HOURS)


def _notice_covers_debit(notices: tuple[NoticeRecord, ...], target_time: datetime) -> bool:
    for notice in notices:
        if notice.covers_debit_at != target_time:
            continue
        age = target_time - notice.sent_at
        if timedelta(hours=NOTICE_MIN_HOURS) <= age <= timedelta(days=NOTICE_MAX_AGE_DAYS):
            return True
    return False


def authorize(
    action: ActionType,
    snapshot: CaseSnapshot,
    now: datetime,
    target_time: datetime | None = None,
) -> Verdict:
    if snapshot.global_kill_switch:
        return Verdict.deny(DenyReason.GLOBAL_KILL_SWITCH)
    if snapshot.merchant_kill_switch:
        return Verdict.deny(DenyReason.MERCHANT_KILL_SWITCH)
    if not snapshot.mandate_active:
        return Verdict.deny(DenyReason.MANDATE_NOT_ACTIVE)
    if snapshot.opted_out:
        return Verdict.deny(DenyReason.OPTED_OUT)

    if action == ActionType.ATTEMPT:
        if target_time is None:
            raise ValueError("ATTEMPT requires target_time")
        if snapshot.attempts_used >= MAX_ATTEMPTS:
            return Verdict.deny(DenyReason.ATTEMPT_BUDGET_EXHAUSTED)
        if not _in_permitted_window(target_time):
            return Verdict.deny(DenyReason.OUTSIDE_EXECUTION_WINDOW)
        if not _notice_covers_debit(snapshot.notices, target_time):
            return Verdict.deny(DenyReason.RBI_NOTICE_NOT_SATISFIED)
        if snapshot.amount > snapshot.afa_threshold and not snapshot.afa_satisfied:
            return Verdict.deny(DenyReason.AFA_REQUIRED_NOT_SATISFIED)
        return Verdict.allow()

    if action == ActionType.NOTIFY:
        if snapshot.contact_count_today >= snapshot.contact_cap:
            return Verdict.deny(DenyReason.CONTACT_CAP_EXCEEDED)
        if snapshot.quiet_hours_active:
            return Verdict.deny(DenyReason.OUTSIDE_QUIET_HOURS)
        return Verdict.allow()

    # IDLE and STOP_AND_ESCALATE carry no independent risk once the
    # kill-switch/mandate/opt-out gates above have passed.
    return Verdict.allow()
