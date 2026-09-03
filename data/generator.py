"""Synthetic payer population generator (docs J.2, docs/DATA_MODEL.md).

Deterministic: `generate_population(seed, n)` called twice with the same
arguments produces byte-identical output. This is what makes the committed
`data/TEST_SPLIT_SHA256` a checkable claim rather than a promise.

Scope note: this module generates the *payer population* only — static,
per-payer attributes and a train/calibration/dev/test split assignment.
Mandate-cycle / event generation is a Phase 3 concern (worker + simulator
interaction) and is deliberately not implemented here (see
docs/DATA_MODEL.md "Rare and adversarial cases").
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml
from app.domain.types import Cause

GLOBAL_SEED = 20260827
N_PAYERS = 10_000

DATA_DIR = Path(__file__).resolve().parent
TAXONOMY_PATH = DATA_DIR / "taxonomy.yaml"
TEST_SPLIT_HASH_PATH = DATA_DIR / "TEST_SPLIT_SHA256"
GENERATED_DIR = DATA_DIR / "generated"

SEGMENTS = ("salaried", "gig", "self_employed", "student")
SEGMENT_WEIGHTS = (0.55, 0.25, 0.15, 0.05)

MEAN_BALANCE_PARAMS: dict[str, tuple[float, float]] = {
    "salaried": (math.log(25_000), 0.50),
    "gig": (math.log(12_000), 0.60),
    "self_employed": (math.log(18_000), 0.70),
    "student": (math.log(4_000), 0.50),
}

VOLATILITY_PARAMS: dict[str, tuple[float, float]] = {
    "salaried": (4.0, 0.15),
    "gig": (2.0, 0.40),
    "self_employed": (3.0, 0.25),
    "student": (3.0, 0.20),
}

ISSUER_SUCCESS_RATES: dict[str, float] = {
    "ISS01": 0.93,
    "ISS02": 0.91,
    "ISS03": 0.90,
    "ISS04": 0.89,
    "ISS05": 0.88,
    "ISS06": 0.87,
    "ISS07": 0.85,
    "ISS08": 0.84,
    "ISS09": 0.82,
    "ISS10": 0.80,
    "ISS11": 0.78,
    "ISS12": 0.55,
}
ISSUERS = tuple(ISSUER_SUCCESS_RATES)

SEGMENT_ANNOYANCE_MULT: dict[str, float] = {
    "salaried": 0.8,
    "gig": 1.0,
    "self_employed": 0.9,
    "student": 1.2,
}

SEGMENT_AMOUNT_MULT: dict[str, float] = {
    "salaried": 1.4,
    "gig": 0.9,
    "self_employed": 1.1,
    "student": 0.5,
}

AFA_AMOUNT_PROBABILITY = 0.04
ADVERSARIAL_PROBABILITY = 0.03
HELD_OUT_PROBABILITY = 0.15

# Cumulative upper bounds over a per-payer hash fraction in [0, 1).
# train 60% / calibration 15% / dev 20% / test 5% — see docs/DATA_MODEL.md
# "Resolving an ambiguity between J.2 and J.5".
SPLIT_CUMULATIVE: tuple[tuple[str, float], ...] = (
    ("train", 0.60),
    ("calibration", 0.75),
    ("dev", 0.95),
    ("test", 1.00),
)


@dataclass(frozen=True)
class Payer:
    payer_id: str
    segment: str
    credit_day: int
    mean_balance: float
    balance_volatility: float
    issuer_code: str
    chronic_fail_propensity: float
    annoyance_sensitivity: float
    mandate_amount: float
    split: str


def _digest_int(*parts: str) -> int:
    h = hashlib.sha256(":".join(parts).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def _payer_rng(seed: int, idx: int) -> random.Random:
    return random.Random(_digest_int(str(seed), "payer", str(idx)))


def _split_fraction(seed: int, payer_id: str) -> float:
    return _digest_int(str(seed), "split", payer_id) / 2**64


def _assign_split(seed: int, payer_id: str) -> str:
    frac = _split_fraction(seed, payer_id)
    for label, upper in SPLIT_CUMULATIVE:
        if frac < upper:
            return label
    return SPLIT_CUMULATIVE[-1][0]


def _sample_segment(rng: random.Random) -> str:
    return rng.choices(SEGMENTS, weights=SEGMENT_WEIGHTS)[0]


def _sample_credit_day(rng: random.Random, segment: str) -> int:
    if segment == "salaried":
        if rng.random() < 0.70:
            return rng.choice([1, 2, 7, 28])
        return rng.randint(1, 28)
    if segment == "gig":
        weekly = {7, 14, 21, 28}
        days = list(range(1, 29))
        weights = [2.5 if d in weekly else 1.0 for d in days]
        return rng.choices(days, weights=weights)[0]
    if segment == "self_employed":
        return rng.randint(1, 28)
    if segment == "student":
        if rng.random() < 0.60:
            return rng.choice([1, 5])
        return rng.randint(1, 28)
    raise ValueError(f"unknown segment: {segment}")


def _sample_mean_balance(rng: random.Random, segment: str) -> float:
    mu, sigma = MEAN_BALANCE_PARAMS[segment]
    return rng.lognormvariate(mu, sigma)


def _sample_volatility(rng: random.Random, segment: str) -> float:
    shape, scale = VOLATILITY_PARAMS[segment]
    return rng.gammavariate(shape, scale)


def _sample_annoyance(rng: random.Random, segment: str) -> float:
    base = rng.betavariate(2.0, 5.0)
    return min(1.0, base * SEGMENT_ANNOYANCE_MULT[segment])


def _sample_amount(rng: random.Random, segment: str) -> float:
    if rng.random() < AFA_AMOUNT_PROBABILITY:
        return round(rng.uniform(15_000.0, 100_000.0), 2)
    mu = math.log(600.0 * SEGMENT_AMOUNT_MULT[segment])
    val = rng.lognormvariate(mu, 0.6)
    return round(min(max(val, 50.0), 14_999.0), 2)


def generate_payer(seed: int, idx: int) -> Payer:
    payer_id = f"PAYER{idx:05d}"
    rng = _payer_rng(seed, idx)
    segment = _sample_segment(rng)
    return Payer(
        payer_id=payer_id,
        segment=segment,
        credit_day=_sample_credit_day(rng, segment),
        mean_balance=_sample_mean_balance(rng, segment),
        balance_volatility=_sample_volatility(rng, segment),
        issuer_code=rng.choice(ISSUERS),
        chronic_fail_propensity=rng.betavariate(1.5, 12.0),
        annoyance_sensitivity=_sample_annoyance(rng, segment),
        mandate_amount=_sample_amount(rng, segment),
        split=_assign_split(seed, payer_id),
    )


def generate_population(seed: int = GLOBAL_SEED, n: int = N_PAYERS) -> list[Payer]:
    return [generate_payer(seed, i) for i in range(n)]


def sealed_test_split_sha256(payers: list[Payer]) -> str:
    ids = sorted(p.payer_id for p in payers if p.split == "test")
    canonical = "\n".join(ids) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_taxonomy(data: dict[str, Any]) -> None:
    expected = {c.value for c in Cause}
    got = set(data["causes"])
    if got != expected:
        raise ValueError(
            f"data/taxonomy.yaml cause keys {sorted(got)} do not match "
            f"domain Cause enum {sorted(expected)}"
        )


def load_taxonomy(path: Path = TAXONOMY_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    _validate_taxonomy(data)
    return data


def sample_raw_decline(
    rng: random.Random, cause: Cause, taxonomy: dict[str, Any]
) -> tuple[str, bool, bool]:
    """Returns (raw_string, held_out, adversarial) per docs J.4."""
    templates: list[str] = taxonomy["causes"][cause.value]
    raw = rng.choice(templates)
    held_out = rng.random() < HELD_OUT_PROBABILITY
    adversarial = rng.random() < ADVERSARIAL_PROBABILITY
    if adversarial:
        raw = raw + rng.choice(taxonomy["adversarial_suffixes"])
    return raw, held_out, adversarial


def _summary(payers: list[Payer]) -> dict[str, Any]:
    by_split: dict[str, int] = {}
    by_segment: dict[str, int] = {}
    for p in payers:
        by_split[p.split] = by_split.get(p.split, 0) + 1
        by_segment[p.segment] = by_segment.get(p.segment, 0) + 1
    return {"total": len(payers), "by_split": by_split, "by_segment": by_segment}


def main() -> None:
    payers = generate_population()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_DIR / "payers.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for p in payers:
            f.write(json.dumps(asdict(p), sort_keys=True) + "\n")

    digest = sealed_test_split_sha256(payers)
    TEST_SPLIT_HASH_PATH.write_text(digest + "\n", encoding="utf-8")

    taxonomy = load_taxonomy()

    summary = _summary(payers)
    print(f"wrote {len(payers)} payers -> {out_path}")
    print(f"split counts: {summary['by_split']}")
    print(f"segment counts: {summary['by_segment']}")
    print(f"test split sha256: {digest}")
    print(f"taxonomy: {len(taxonomy['causes'])} causes validated against domain Cause enum")


if __name__ == "__main__":
    main()
