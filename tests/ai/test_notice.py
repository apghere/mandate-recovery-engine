from __future__ import annotations

import pytest
from app.ai import notice
from app.ai.client import LlmTextResponse, LlmUnavailable

VARS = notice.NoticeVariables(
    merchant_name="Acme Subscriptions",
    amount="Rs.500",
    debit_date="15 September 2026",
    debit_time="02:00",
    mandate_ref="MAND-1234",
    reason="insufficient funds",
    channel="sms",
)


def test_fallback_template_is_always_self_consistent() -> None:
    """No mocking -- the static template must pass its own validator by
    construction, since it's the last line of defense when everything
    else fails."""
    result = notice.generate_notice(VARS)
    assert result.generated_by in ("llm", "template")
    assert result.validator_result.valid


def test_llm_unavailable_falls_straight_to_template(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> LlmTextResponse:
        raise LlmUnavailable("no key")

    monkeypatch.setattr(notice, "complete", _raise)
    result = notice.generate_notice(VARS)
    assert result.generated_by == "template"
    assert result.validator_result.valid
    assert not result.repaired


def test_valid_llm_draft_is_accepted_on_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    good_draft = (
        "Acme Subscriptions: Rs.500 will be debited on mandate MAND-1234 on "
        "15 September 2026 at 02:00 due to insufficient funds. Reply STOP to opt out."
    )
    calls = []

    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        calls.append(user)
        return LlmTextResponse(text=good_draft, model="claude-haiku-4-5")

    monkeypatch.setattr(notice, "complete", _fake_complete)
    result = notice.generate_notice(VARS)
    assert result.generated_by == "llm"
    assert not result.repaired
    assert result.body == good_draft
    assert len(calls) == 1  # no repair call needed


def test_invalid_first_draft_triggers_one_repair_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_draft = "URGENT: your payment failed, contact legal immediately!"
    good_repair = (
        "Acme Subscriptions: Rs.500 will be debited on mandate MAND-1234 on "
        "15 September 2026 at 02:00 due to insufficient funds. Reply STOP to opt out."
    )
    calls = []

    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        calls.append(user)
        if len(calls) == 1:
            return LlmTextResponse(text=bad_draft, model="claude-haiku-4-5")
        return LlmTextResponse(text=good_repair, model="claude-haiku-4-5")

    monkeypatch.setattr(notice, "complete", _fake_complete)
    result = notice.generate_notice(VARS)
    assert result.generated_by == "llm"
    assert result.repaired
    assert result.body == good_repair
    assert len(calls) == 2
    # The repair prompt must actually carry the validator's error signal.
    assert "rejected for" in calls[1]


def test_two_bad_drafts_fall_back_to_template(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_draft = "URGENT: contact our legal team immediately, final notice!"
    calls = []

    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        calls.append(user)
        return LlmTextResponse(text=bad_draft, model="claude-haiku-4-5")

    monkeypatch.setattr(notice, "complete", _fake_complete)
    result = notice.generate_notice(VARS)
    assert result.generated_by == "template"
    assert result.validator_result.valid
    assert len(calls) == 2  # first draft + one repair attempt, both rejected


def test_repair_never_happens_when_first_draft_already_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_draft = (
        "Acme Subscriptions: Rs.500 will be debited on mandate MAND-1234 on "
        "15 September 2026 at 02:00 due to insufficient funds. Reply STOP to opt out."
    )
    call_count = 0

    def _fake_complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
        nonlocal call_count
        call_count += 1
        return LlmTextResponse(text=good_draft, model="claude-haiku-4-5")

    monkeypatch.setattr(notice, "complete", _fake_complete)
    notice.generate_notice(VARS)
    assert call_count == 1
