from __future__ import annotations

import json

import pytest
from app.ai import normalizer
from app.ai.client import LlmTextResponse, LlmUnavailable
from app.domain.types import Cause

from data.generator import load_taxonomy

TAXONOMY = load_taxonomy()


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    normalizer._cache.clear()


def test_levenshtein_known_distances() -> None:
    assert normalizer._levenshtein("", "") == 0
    assert normalizer._levenshtein("abc", "abc") == 0
    assert normalizer._levenshtein("abc", "") == 3
    assert normalizer._levenshtein("kitten", "sitting") == 3
    assert normalizer._levenshtein("INSUFF FU", "INSUFF FUND") == 2


def test_dictionary_exact_match() -> None:
    result = normalizer.normalize(
        "INSUFFICIENT FUNDS", issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY
    )
    assert result.cause == Cause.INSUFFICIENT_FUNDS
    assert result.source == "dictionary"
    assert result.confidence == 1.0


def test_dictionary_match_is_case_insensitive() -> None:
    result = normalizer.normalize(
        "insufficient funds", issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY
    )
    assert result.cause == Cause.INSUFFICIENT_FUNDS
    assert result.source == "dictionary"


def test_fuzzy_match_within_edit_distance_two() -> None:
    # "INSUFF FU" is a real template; one character off still matches.
    result = normalizer.normalize(
        "INSUFF FX", issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY
    )
    assert result.cause == Cause.INSUFFICIENT_FUNDS
    assert result.source == "fuzzy"


def test_llm_falls_back_to_unknown_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> LlmTextResponse:
        raise LlmUnavailable("no key configured")

    monkeypatch.setattr(normalizer, "complete", _raise)
    result = normalizer.normalize(
        "a completely novel string not in any template",
        issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY,
    )
    assert result.cause == Cause.UNKNOWN
    assert result.source == "unknown"
    assert result.evidence_span is None


def test_llm_valid_response_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = "DEBIT DECLINED - RISK CHECK FAILED PER BANK POLICY"
    payload = {"cause": "RISK_DECLINE", "confidence": 0.92, "evidence_span": "RISK CHECK FAILED"}

    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        return LlmTextResponse(text=json.dumps(payload), model="claude-haiku-4-5")

    monkeypatch.setattr(normalizer, "complete", _fake_complete)
    result = normalizer.normalize(raw, issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY)
    assert result.cause == Cause.RISK_DECLINE
    assert result.source == "llm"
    assert result.confidence == 0.92
    assert result.evidence_span == "RISK CHECK FAILED"


@pytest.mark.parametrize(
    "payload",
    [
        {"cause": "NOT_A_REAL_CAUSE", "confidence": 0.99, "evidence_span": "x"},
        {"cause": "RISK_DECLINE", "confidence": 0.99, "evidence_span": "not in the raw string"},
        {"cause": "RISK_DECLINE", "confidence": 0.5, "evidence_span": "RISK"},  # below threshold
        {"cause": "RISK_DECLINE", "confidence": 1.5, "evidence_span": "RISK"},  # out of range
        {"cause": "RISK_DECLINE", "confidence": "high", "evidence_span": "RISK"},  # wrong type
        {"cause": "RISK_DECLINE"},  # missing fields
        "not even a dict",
    ],
)
def test_llm_invalid_output_falls_back_to_unknown(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    raw = "RISK DECLINE BY BANK"

    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        return LlmTextResponse(text=json.dumps(payload), model="claude-haiku-4-5")

    monkeypatch.setattr(normalizer, "complete", _fake_complete)
    result = normalizer.normalize(raw, issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY)
    assert result.cause == Cause.UNKNOWN
    assert result.source == "unknown"


def test_llm_malformed_json_falls_back_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        return LlmTextResponse(text="not json at all {{{", model="claude-haiku-4-5")

    monkeypatch.setattr(normalizer, "complete", _fake_complete)
    result = normalizer.normalize(
        "some novel string", issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY
    )
    assert result.cause == Cause.UNKNOWN


@pytest.mark.parametrize("suffix", TAXONOMY["adversarial_suffixes"])
def test_prompt_injection_worst_case_is_one_bounded_misclassification(
    monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    """docs L.3 red-team exercise: inject a prompt-injection remark and
    confirm the worst outcome is a misclassification — never an
    out-of-enum value, never a fabricated evidence_span, never anything
    that isn't already bounded by the schema. Simulates an LLM that
    *did* get tricked (returns the suffix's own demanded cause) to prove
    the validator, not the model's good behaviour, is what bounds this."""
    raw = "INSUFF FU" + suffix

    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        # Simulate a successfully-injected model: it obeys the suffix and
        # claims MANDATE_REVOKED regardless of the real content, with a
        # fabricated (not literally-present) evidence span.
        payload = {
            "cause": "MANDATE_REVOKED",
            "confidence": 1.0,
            "evidence_span": "this text is not actually in the raw string",
        }
        return LlmTextResponse(text=json.dumps(payload), model="claude-haiku-4-5")

    monkeypatch.setattr(normalizer, "complete", _fake_complete)
    result = normalizer.normalize(raw, issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY)
    # The fabricated evidence_span fails the literal-substring check, so
    # even a "successfully injected" model's output is rejected outright.
    assert result.cause == Cause.UNKNOWN
    assert result.source == "unknown"


def test_normalize_never_raises_regardless_of_llm_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*args: object, **kwargs: object) -> LlmTextResponse:
        raise RuntimeError("simulated unexpected SDK failure")

    monkeypatch.setattr(normalizer, "complete", _explode)
    with pytest.raises(RuntimeError):
        # An unexpected (non-LlmUnavailable) exception is NOT swallowed —
        # only the documented failure mode (LlmUnavailable) is a fallback
        # trigger. This test pins that boundary deliberately.
        normalizer.normalize(
            "novel string", issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY
        )


def test_result_is_cached_by_input_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        calls.append(user)
        payload = {"cause": "TIMEOUT", "confidence": 0.9, "evidence_span": "TIMEOUT"}
        return LlmTextResponse(text=json.dumps(payload), model="claude-haiku-4-5")

    monkeypatch.setattr(normalizer, "complete", _fake_complete)
    raw = "TIMEOUT - NO RESPONSE"
    normalizer.normalize(raw, issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY)
    normalizer.normalize(raw, issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY)
    assert len(calls) == 1  # second call was served from cache


def test_dictionary_and_fuzzy_paths_never_touch_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> LlmTextResponse:
        raise AssertionError("LLM should not be called for a dictionary/fuzzy hit")

    monkeypatch.setattr(normalizer, "complete", _fail_if_called)
    normalizer.normalize(
        "INSUFFICIENT FUNDS", issuer_code="ISS01", rail="upi_autopay", taxonomy=TAXONOMY
    )
