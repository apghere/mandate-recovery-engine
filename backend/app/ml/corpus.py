"""Labeled training corpus generator (docs §N Day 3: "train GBM on
train; fit isotonic on calibration").

Draws payers from a given split of data.generator's population and, for
each, samples several candidate (day-of-month, hour, attempt-sequence-no)
combinations a planner would actually consider — attempts 2-4 only, since
attempt 1 is fired by the external flow outside MRE (see app/ingest.py's
module docstring) and is never something MRE scores. Labels are drawn from
the *simulator's* timing-sensitive decide_outcome (simulator/decline.py),
using hard Bernoulli draws rather than the underlying probability itself —
a real dataset only ever has 0/1 outcomes, and training on the ground-truth
probability directly would be cheating the whole point of calibration.

This corpus is intentionally not persisted: it's fast enough (seconds) to
regenerate deterministically from a seed every time `make train` runs, so
there's nothing to keep in sync or go stale.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date

from app.domain.types import CAUSE_DISPOSITION, MandateSnapshot
from app.ml.features import FeatureRow, assemble_features
from data.generator import ISSUER_SUCCESS_RATES, Payer, generate_population
from simulator.app import PERMITTED_WINDOW_HOURS
from simulator.decline import FAILURE_CAUSE_WEIGHTS, decide_outcome

CORPUS_SEED = 20260901
SAMPLES_PER_PAYER = 4
ATTEMPT_SEQUENCE_CHOICES = (2, 3, 4)  # attempt 1 is external — see module docstring
_REFERENCE_MONTH_YEAR = (2026, 9)  # arbitrary fixed month; only weekday() is used

_ALLOWED_HOURS = tuple(h for window in PERMITTED_WINDOW_HOURS for h in window)


@dataclass(frozen=True)
class CorpusRow:
    snapshot: MandateSnapshot
    label: int  # 1 = success, 0 = failure


def _digest_rng(seed: int, payer_id: str, sample_idx: int) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{payer_id}:{sample_idx}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _sample_snapshot_and_label(rng: random.Random, payer: Payer) -> CorpusRow:
    day_of_month = rng.randint(1, 28)
    hour = rng.choice(_ALLOWED_HOURS)
    attempt_sequence_no = rng.choice(ATTEMPT_SEQUENCE_CHOICES)
    consecutive_prior_failures = rng.randint(0, attempt_sequence_no - 1)
    notices_sent_this_cycle = max(0, attempt_sequence_no - 1)

    cause = rng.choices(list(FAILURE_CAUSE_WEIGHTS), weights=list(FAILURE_CAUSE_WEIGHTS.values()))[
        0
    ]
    disposition = CAUSE_DISPOSITION[cause]

    year, month = _REFERENCE_MONTH_YEAR
    day_of_week = date(year, month, day_of_month).weekday()

    snapshot = MandateSnapshot(
        cause=cause,
        disposition=disposition,
        attempt_sequence_no=attempt_sequence_no,
        hours_since_last_failure=rng.uniform(1.0, 168.0),
        day_of_month=day_of_month,
        days_to_credit_day=(payer.credit_day - day_of_month) % 28,
        slot_of_day=0 if hour < 12 else 1,
        day_of_week=day_of_week,
        amount=int(round(payer.mandate_amount)),
        amount_over_historical_mean=1.0,  # no per-cycle amount history modelled yet
        mandate_age_days=rng.randint(30, 720),
        payer_prior_success_rate=min(
            1.0,
            max(
                0.0,
                ISSUER_SUCCESS_RATES.get(payer.issuer_code, 0.85) + rng.uniform(-0.1, 0.1),
            ),
        ),
        consecutive_prior_failures=consecutive_prior_failures,
        issuer_historical_success_rate=ISSUER_SUCCESS_RATES.get(payer.issuer_code, 0.85),
        issuer_downtime_active=rng.random() < 0.03,
        rail="upi_autopay",
        segment_proxy=payer.segment,
        notices_sent_this_cycle=notices_sent_this_cycle,
        days_since_last_notice=(rng.uniform(1.0, 3.0) if notices_sent_this_cycle else None),
    )

    outcome, _raw_reason = decide_outcome(
        rng,
        issuer_code=payer.issuer_code,
        chronic_fail_propensity=payer.chronic_fail_propensity,
        mean_balance=payer.mean_balance,
        balance_volatility=payer.balance_volatility,
        day_of_month=day_of_month,
        credit_day=payer.credit_day,
        amount=payer.mandate_amount,
    )
    return CorpusRow(snapshot=snapshot, label=1 if outcome == "success" else 0)


def generate_corpus(
    split: str, *, seed: int = CORPUS_SEED, samples_per_payer: int = SAMPLES_PER_PAYER
) -> list[CorpusRow]:
    payers = [p for p in generate_population(seed=seed) if p.split == split]
    rows: list[CorpusRow] = []
    for payer in payers:
        for sample_idx in range(samples_per_payer):
            rng = _digest_rng(seed, payer.payer_id, sample_idx)
            rows.append(_sample_snapshot_and_label(rng, payer))
    return rows


def corpus_to_features_and_labels(rows: list[CorpusRow]) -> tuple[list[FeatureRow], list[int]]:
    features = [assemble_features(r.snapshot) for r in rows]
    labels = [r.label for r in rows]
    return features, labels
