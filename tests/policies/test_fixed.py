from __future__ import annotations

from datetime import date, timedelta

from app.policies.fixed import ATTEMPT_OFFSET_DAYS, NOTICE_LEAD_DAYS, compute_fixed_schedule


def test_produces_one_notify_and_one_attempt_per_offset() -> None:
    steps = compute_fixed_schedule(date(2026, 9, 1))
    notify_steps = [s for s in steps if s.step_type == "notify"]
    attempt_steps = [s for s in steps if s.step_type == "attempt"]
    assert len(attempt_steps) == len(notify_steps) == len(ATTEMPT_OFFSET_DAYS)


def test_each_notify_precedes_its_attempt_by_the_lead_time() -> None:
    steps = compute_fixed_schedule(date(2026, 9, 1))
    notifies = sorted(s.scheduled_for for s in steps if s.step_type == "notify")
    attempts = sorted(s.scheduled_for for s in steps if s.step_type == "attempt")
    for notify_at, attempt_at in zip(notifies, attempts, strict=True):
        assert attempt_at - notify_at == timedelta(days=NOTICE_LEAD_DAYS)


def test_attempt_days_match_the_configured_offsets() -> None:
    due_date = date(2026, 9, 1)
    steps = compute_fixed_schedule(due_date)
    attempt_days = sorted(s.scheduled_for.date() for s in steps if s.step_type == "attempt")
    expected = sorted(due_date + timedelta(days=o) for o in ATTEMPT_OFFSET_DAYS)
    assert attempt_days == expected


def test_is_deterministic() -> None:
    due_date = date(2026, 9, 1)
    assert compute_fixed_schedule(due_date) == compute_fixed_schedule(due_date)
