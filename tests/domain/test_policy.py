from dataclasses import replace
from datetime import datetime

from app.domain.policy import authorize
from app.domain.types import (
    MAX_ATTEMPTS,
    ActionType,
    DenyReason,
    NoticeRecord,
    Verdict,
)
from hypothesis import given

from tests.domain.strategies import case_snapshots

NOW = datetime(2026, 1, 10, 3, 0)
NON_PEAK_TARGET = datetime(2026, 1, 11, 2, 0)


def base_snapshot(**overrides):
    from app.domain.types import CaseSnapshot, CaseState

    defaults = dict(
        state=CaseState.SCHEDULED,
        attempts_used=0,
        mandate_active=True,
        opted_out=False,
        amount=1_000,
        afa_threshold=15_000,
        afa_satisfied=False,
        notices=(
            NoticeRecord(sent_at=datetime(2026, 1, 10, 2, 0), covers_debit_at=NON_PEAK_TARGET),
        ),
        contact_count_today=0,
        contact_cap=3,
        quiet_hours_active=False,
        merchant_kill_switch=False,
        global_kill_switch=False,
    )
    defaults.update(overrides)
    return CaseSnapshot(**defaults)


def test_allows_a_well_formed_attempt() -> None:
    v = authorize(ActionType.ATTEMPT, base_snapshot(), NOW, NON_PEAK_TARGET)
    assert v == Verdict.allow()


def test_denies_attempt_over_budget() -> None:
    snap = base_snapshot(attempts_used=MAX_ATTEMPTS)
    v = authorize(ActionType.ATTEMPT, snap, NOW, NON_PEAK_TARGET)
    assert v == Verdict.deny(DenyReason.ATTEMPT_BUDGET_EXHAUSTED)


def test_denies_attempt_outside_execution_window() -> None:
    peak_target = datetime(2026, 1, 11, 14, 0)
    sent = datetime(2026, 1, 10, 13, 0)
    covering_notice = NoticeRecord(sent_at=sent, covers_debit_at=peak_target)
    snap = base_snapshot(notices=(covering_notice,))
    v = authorize(ActionType.ATTEMPT, snap, NOW, peak_target)
    assert v == Verdict.deny(DenyReason.OUTSIDE_EXECUTION_WINDOW)


def test_denies_attempt_without_a_covering_notice() -> None:
    snap = base_snapshot(notices=())
    v = authorize(ActionType.ATTEMPT, snap, NOW, NON_PEAK_TARGET)
    assert v == Verdict.deny(DenyReason.RBI_NOTICE_NOT_SATISFIED)


def test_denies_attempt_with_a_too_fresh_notice() -> None:
    too_fresh = NoticeRecord(sent_at=NON_PEAK_TARGET, covers_debit_at=NON_PEAK_TARGET)
    snap = base_snapshot(notices=(too_fresh,))
    v = authorize(ActionType.ATTEMPT, snap, NOW, NON_PEAK_TARGET)
    assert v == Verdict.deny(DenyReason.RBI_NOTICE_NOT_SATISFIED)


def test_denies_attempt_above_afa_threshold_when_unsatisfied() -> None:
    snap = base_snapshot(amount=20_000, afa_threshold=15_000, afa_satisfied=False)
    v = authorize(ActionType.ATTEMPT, snap, NOW, NON_PEAK_TARGET)
    assert v == Verdict.deny(DenyReason.AFA_REQUIRED_NOT_SATISFIED)


def test_allows_attempt_above_afa_threshold_when_satisfied() -> None:
    snap = base_snapshot(amount=20_000, afa_threshold=15_000, afa_satisfied=True)
    v = authorize(ActionType.ATTEMPT, snap, NOW, NON_PEAK_TARGET)
    assert v == Verdict.allow()


def test_denies_notify_over_contact_cap() -> None:
    snap = base_snapshot(contact_count_today=3, contact_cap=3)
    v = authorize(ActionType.NOTIFY, snap, NOW)
    assert v == Verdict.deny(DenyReason.CONTACT_CAP_EXCEEDED)


def test_denies_notify_in_quiet_hours() -> None:
    snap = base_snapshot(quiet_hours_active=True)
    v = authorize(ActionType.NOTIFY, snap, NOW)
    assert v == Verdict.deny(DenyReason.OUTSIDE_QUIET_HOURS)


def test_global_kill_switch_overrides_everything() -> None:
    snap = base_snapshot(global_kill_switch=True, opted_out=True, mandate_active=False)
    v = authorize(ActionType.ATTEMPT, snap, NOW, NON_PEAK_TARGET)
    assert v == Verdict.deny(DenyReason.GLOBAL_KILL_SWITCH)


def test_merchant_kill_switch_denies() -> None:
    snap = base_snapshot(merchant_kill_switch=True)
    v = authorize(ActionType.ATTEMPT, snap, NOW, NON_PEAK_TARGET)
    assert v == Verdict.deny(DenyReason.MERCHANT_KILL_SWITCH)


_ALL_ACTIONS = (
    ActionType.ATTEMPT,
    ActionType.NOTIFY,
    ActionType.IDLE,
    ActionType.STOP_AND_ESCALATE,
)


def test_revoked_mandate_denies_every_action() -> None:
    snap = base_snapshot(mandate_active=False)
    for action in _ALL_ACTIONS:
        v = authorize(action, snap, NOW, NON_PEAK_TARGET)
        assert v == Verdict.deny(DenyReason.MANDATE_NOT_ACTIVE)


def test_opt_out_denies_every_action() -> None:
    snap = base_snapshot(opted_out=True)
    for action in _ALL_ACTIONS:
        v = authorize(action, snap, NOW, NON_PEAK_TARGET)
        assert v == Verdict.deny(DenyReason.OPTED_OUT)


def test_idle_and_escalate_pass_once_gates_clear() -> None:
    snap = base_snapshot()
    assert authorize(ActionType.IDLE, snap, NOW) == Verdict.allow()
    assert authorize(ActionType.STOP_AND_ESCALATE, snap, NOW) == Verdict.allow()


# --- Property tests -------------------------------------------------------


@given(snapshot=case_snapshots())
def test_authorize_is_pure_and_deterministic(snapshot) -> None:
    """docs §Q: 'authorize() purity' — same inputs, same output, no mutation."""
    before = replace(snapshot)
    v1 = authorize(ActionType.ATTEMPT, snapshot, NOW, NON_PEAK_TARGET)
    v2 = authorize(ActionType.ATTEMPT, snapshot, NOW, NON_PEAK_TARGET)
    assert v1 == v2
    assert snapshot == before  # frozen dataclass: no in-place mutation possible


@given(snapshot=case_snapshots())
def test_authorize_never_allows_attempt_over_budget(snapshot) -> None:
    """docs §Q: 'no event sequence ever yields attempts_used > 4' — the
    domain-level guarantee is that authorize() never says Allow once the
    budget is spent, for any other combination of fields."""
    if snapshot.attempts_used < MAX_ATTEMPTS:
        return
    v = authorize(ActionType.ATTEMPT, snapshot, NOW, NON_PEAK_TARGET)
    assert v.allowed is False
