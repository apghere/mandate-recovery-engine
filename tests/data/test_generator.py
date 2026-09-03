from __future__ import annotations

import random

from app.domain.types import Cause

from data.generator import (
    GLOBAL_SEED,
    TEST_SPLIT_HASH_PATH,
    Payer,
    generate_population,
    load_taxonomy,
    sample_raw_decline,
    sealed_test_split_sha256,
)


def test_generate_population_is_deterministic() -> None:
    a = generate_population(seed=1, n=200)
    b = generate_population(seed=1, n=200)
    assert a == b


def test_different_seeds_produce_different_populations() -> None:
    a = generate_population(seed=1, n=200)
    b = generate_population(seed=2, n=200)
    assert a != b


def test_population_size_and_uniqueness() -> None:
    payers = generate_population(seed=1, n=500)
    assert len(payers) == 500
    assert len({p.payer_id for p in payers}) == 500


def test_split_shares_are_roughly_correct() -> None:
    payers = generate_population(seed=1, n=10_000)
    counts: dict[str, int] = {}
    for p in payers:
        counts[p.split] = counts.get(p.split, 0) + 1
    assert 0.55 <= counts.get("train", 0) / 10_000 <= 0.65
    assert 0.10 <= counts.get("calibration", 0) / 10_000 <= 0.20
    assert 0.15 <= counts.get("dev", 0) / 10_000 <= 0.25
    assert 0.02 <= counts.get("test", 0) / 10_000 <= 0.08


def test_segment_shares_are_roughly_correct() -> None:
    payers = generate_population(seed=1, n=10_000)
    counts: dict[str, int] = {}
    for p in payers:
        counts[p.segment] = counts.get(p.segment, 0) + 1
    assert 0.50 <= counts.get("salaried", 0) / 10_000 <= 0.60
    assert 0.20 <= counts.get("gig", 0) / 10_000 <= 0.30
    assert 0.10 <= counts.get("self_employed", 0) / 10_000 <= 0.20
    assert 0.02 <= counts.get("student", 0) / 10_000 <= 0.08


def test_amount_afa_threshold_share_is_roughly_four_percent() -> None:
    payers = generate_population(seed=1, n=10_000)
    above = sum(1 for p in payers if p.mandate_amount > 15_000)
    assert 0.02 <= above / 10_000 <= 0.07


def test_gig_segment_has_higher_mean_volatility_than_salaried() -> None:
    payers = generate_population(seed=1, n=10_000)
    gig = [p.balance_volatility for p in payers if p.segment == "gig"]
    salaried = [p.balance_volatility for p in payers if p.segment == "salaried"]
    assert sum(gig) / len(gig) > sum(salaried) / len(salaried)


def test_committed_test_split_hash_matches_default_population() -> None:
    """The load-bearing test: proves the sealed test split hasn't drifted
    from what's committed in data/TEST_SPLIT_SHA256 (docs J.5)."""
    committed = TEST_SPLIT_HASH_PATH.read_text(encoding="utf-8").strip()
    payers = generate_population(seed=GLOBAL_SEED)
    assert sealed_test_split_sha256(payers) == committed


def test_load_taxonomy_validates_against_domain_cause_enum() -> None:
    taxonomy = load_taxonomy()
    assert set(taxonomy["causes"]) == {c.value for c in Cause}


def test_sample_raw_decline_is_deterministic_given_rng_state() -> None:
    taxonomy = load_taxonomy()
    a = sample_raw_decline(random.Random(42), Cause.INSUFFICIENT_FUNDS, taxonomy)
    b = sample_raw_decline(random.Random(42), Cause.INSUFFICIENT_FUNDS, taxonomy)
    assert a == b


def test_sample_raw_decline_returns_a_template_or_adversarial_variant() -> None:
    taxonomy = load_taxonomy()
    templates = set(taxonomy["causes"][Cause.INSUFFICIENT_FUNDS.value])
    rng = random.Random(7)
    for _ in range(200):
        raw, _held_out, adversarial = sample_raw_decline(rng, Cause.INSUFFICIENT_FUNDS, taxonomy)
        if adversarial:
            assert any(raw.startswith(t) for t in templates)
        else:
            assert raw in templates


def test_payer_is_frozen_dataclass_instance() -> None:
    payers = generate_population(seed=1, n=1)
    assert isinstance(payers[0], Payer)
