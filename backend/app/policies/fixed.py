"""P0 — the fixed-schedule baseline (docs G.2 M6).

Not the product; the strawman it's benchmarked against. Reimplements the
D+1/D+3/D+7 cadence merchants copy from American dunning guides (docs
E.1 "Current approach", I.4, R) faithfully — three attempts, each
preceded by a same-hour notice one day earlier (satisfying the >=24h RBI
notice rule with room to spare), using the same allowed execution hour the
simulator enforces independently. No AI, no optimization: this is the
insurance-policy baseline that proves the loop works end-to-end with zero
intelligence layered on top (docs N Day 2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

# Matches simulator.app.ALLOWED_SLOT_HOURS's first (non-peak) slot — the
# fixed baseline doesn't reason about slots at all, it just picks one.
ATTEMPT_HOUR = 2
ATTEMPT_OFFSET_DAYS: tuple[int, ...] = (1, 3, 7)
NOTICE_LEAD_DAYS = 1

POLICY_VERSION = "P0-fixed-schedule-v1"


@dataclass(frozen=True)
class ScheduledStep:
    step_type: str  # "notify" | "attempt"
    scheduled_for: datetime
    # Only meaningful for "notify" steps: the exact attempt time this
    # notice covers (docs I.10's freshness check is an *exact* match, not
    # "within N days of the most recent notify"). Explicit rather than
    # assumed, because not every policy pairs notify with an attempt at a
    # fixed offset — see app/policies/mre.py, which is free to notify
    # early and wait for a better slot.
    covers_debit_at: datetime | None = None


def compute_fixed_schedule(due_date: date) -> list[ScheduledStep]:
    steps: list[ScheduledStep] = []
    for offset in ATTEMPT_OFFSET_DAYS:
        attempt_day = due_date + timedelta(days=offset)
        notify_day = attempt_day - timedelta(days=NOTICE_LEAD_DAYS)
        attempt_at = datetime.combine(attempt_day, time(ATTEMPT_HOUR, tzinfo=UTC))
        steps.append(
            ScheduledStep(
                "notify",
                datetime.combine(notify_day, time(ATTEMPT_HOUR, tzinfo=UTC)),
                covers_debit_at=attempt_at,
            )
        )
        steps.append(ScheduledStep("attempt", attempt_at))
    return steps
