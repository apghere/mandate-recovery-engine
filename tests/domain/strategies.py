"""Shared Hypothesis strategies for domain-core property tests."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.types import CaseSnapshot, NoticeRecord
from hypothesis import strategies as st

_BASE = datetime(2026, 1, 1)

datetimes = st.integers(min_value=0, max_value=60 * 24 * 60).map(
    lambda minutes: _BASE + timedelta(minutes=minutes)
)


@st.composite
def notice_records(draw: st.DrawFn) -> NoticeRecord:
    covers = draw(datetimes)
    sent = draw(datetimes)
    return NoticeRecord(sent_at=sent, covers_debit_at=covers)


@st.composite
def case_snapshots(draw: st.DrawFn) -> CaseSnapshot:
    afa_threshold = draw(st.sampled_from([15_000, 100_000]))
    from app.domain.types import CaseState

    return CaseSnapshot(
        state=draw(st.sampled_from(list(CaseState))),
        attempts_used=draw(st.integers(min_value=0, max_value=6)),
        mandate_active=draw(st.booleans()),
        opted_out=draw(st.booleans()),
        amount=draw(st.integers(min_value=1, max_value=500_000)),
        afa_threshold=afa_threshold,
        afa_satisfied=draw(st.booleans()),
        notices=draw(st.lists(notice_records(), max_size=3).map(tuple)),
        contact_count_today=draw(st.integers(min_value=0, max_value=5)),
        contact_cap=draw(st.integers(min_value=0, max_value=3)),
        quiet_hours_active=draw(st.booleans()),
        merchant_kill_switch=draw(st.booleans()),
        global_kill_switch=draw(st.booleans()),
    )
