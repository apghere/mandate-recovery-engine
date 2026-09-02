"""P0b — the deterministic-lookup-table baseline (docs §T red-team item
2: "deterministic code could replace the ML model... a lookup table over
(cause x day-of-month) would capture a lot. Fix, and do it on Day 6:
ship that lookup table as a fifth baseline, P0b, and report it. If the
GBM beats it by little, say so and note that the planner is where the
value lives. Pre-empting your critic's best argument by shipping it as a
baseline is the strongest possible response.").

No model, no calibration, no training in the ML sense: `fit_lookup_table`
just groups the exact same labeled corpus `app/ml/train.py` trains the
GBM on (`app/ml/corpus.py::generate_corpus`, `train` split only — same
split discipline) by `(cause, day_of_month)` and averages the observed
success label in each bucket. `score_slots_lookup` is a drop-in
replacement for `app/ml/inference.py::score_slots`'s output shape (same
`tuple[float, ...]` over slots) so it can be fed directly into the
existing `compute_greedy_schedule` (`app/policies/greedy.py`) with zero
changes to the scheduling logic itself — the *only* thing this baseline
swaps out relative to `greedy` is the source of P(success). That is
deliberate: it isolates exactly what §T item 2 asks about ("is the
calibrated model worth more than a simple table?"), the same way `greedy`
vs `mre` isolates what the DP is worth over naive lookahead.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from app.domain.types import Cause
from app.ml.corpus import CorpusRow
from app.ml.inference import slot_datetime

# A (cause, day_of_month) bucket with fewer than this many labeled
# observations in the train split falls back to the cause-level rate
# (and, if that's also sparse, the global rate) rather than reporting a
# noisy small-sample average as if it were trustworthy -- the same
# small-sample-honesty concern app/ml/calibrate.py's reliability diagram
# already annotates bin counts for for the same reason.
MIN_BUCKET_SAMPLES = 20
MIN_CAUSE_SAMPLES = 50


@dataclass(frozen=True)
class LookupTable:
    by_cause_day: dict[tuple[Cause, int], float]
    by_cause: dict[Cause, float]
    global_rate: float
    bucket_counts: dict[tuple[Cause, int], int]
    cause_counts: dict[Cause, int]


def fit_lookup_table(rows: list[CorpusRow]) -> LookupTable:
    bucket_sum: dict[tuple[Cause, int], int] = defaultdict(int)
    bucket_n: dict[tuple[Cause, int], int] = defaultdict(int)
    cause_sum: dict[Cause, int] = defaultdict(int)
    cause_n: dict[Cause, int] = defaultdict(int)
    global_sum = 0
    global_n = 0

    for row in rows:
        key = (row.snapshot.cause, row.snapshot.day_of_month)
        bucket_sum[key] += row.label
        bucket_n[key] += 1
        cause_sum[row.snapshot.cause] += row.label
        cause_n[row.snapshot.cause] += 1
        global_sum += row.label
        global_n += 1

    global_rate = global_sum / global_n if global_n else 0.5
    by_cause = {c: cause_sum[c] / cause_n[c] for c in cause_n if cause_n[c] > 0}
    by_cause_day = {k: bucket_sum[k] / bucket_n[k] for k in bucket_n if bucket_n[k] > 0}

    return LookupTable(
        by_cause_day=by_cause_day,
        by_cause=by_cause,
        global_rate=global_rate,
        bucket_counts=dict(bucket_n),
        cause_counts=dict(cause_n),
    )


def lookup_rate(table: LookupTable, *, cause: Cause, day_of_month: int) -> float:
    """Three-level backoff: (cause, day) if it has enough samples, else
    cause-level if that has enough samples, else the global rate. Never
    raises on an unseen combination."""
    key = (cause, day_of_month)
    if table.bucket_counts.get(key, 0) >= MIN_BUCKET_SAMPLES:
        return table.by_cause_day[key]
    if table.cause_counts.get(cause, 0) >= MIN_CAUSE_SAMPLES:
        return table.by_cause.get(cause, table.global_rate)
    return table.global_rate


def score_slots_lookup(
    table: LookupTable, *, start_date: date, n_slots: int, cause: Cause
) -> tuple[float, ...]:
    """Same output shape as app/ml/inference.py::score_slots — a
    calibrated-probability-shaped tuple over slots — but every value is a
    plain table lookup, not a model inference call."""
    return tuple(
        lookup_rate(table, cause=cause, day_of_month=slot_datetime(start_date, t).day)
        for t in range(n_slots)
    )
