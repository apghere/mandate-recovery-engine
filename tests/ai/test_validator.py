from __future__ import annotations

from app.ai.validator import validate_notice

REQUIRED = {
    "merchant_name": "Acme Subscriptions",
    "amount": "Rs.500",
    "debit_date": "15 September 2026",
    "debit_time": "02:00",
    "mandate_ref": "MAND-1234",
    "reason": "insufficient funds",
}
WHITELIST = {"500", "15", "2026", "02", "00", "1234"}


def _valid_body() -> str:
    return (
        "Acme Subscriptions: a payment of Rs.500 on mandate MAND-1234 will be "
        "attempted on 15 September 2026 at 02:00 (insufficient funds). "
        "Reply STOP to opt out."
    )


def test_a_correctly_grounded_notice_is_valid() -> None:
    result = validate_notice(
        _valid_body(), channel="sms", whitelist=WHITELIST, required_present=REQUIRED
    )
    assert result.valid
    assert result.errors == []


def test_missing_required_field_is_rejected() -> None:
    body = _valid_body().replace("MAND-1234", "")
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid
    assert any("mandate_ref" in e for e in result.errors)


def test_missing_opt_out_instruction_is_rejected() -> None:
    body = _valid_body().replace("Reply STOP to opt out.", "")
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid
    assert any("opt-out" in e for e in result.errors)


def test_ungrounded_hallucinated_amount_is_rejected() -> None:
    body = _valid_body().replace("Rs.500", "Rs.999")
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid
    assert any("999" in e for e in result.errors)


def test_ungrounded_extra_number_is_rejected_even_if_real_fields_present() -> None:
    body = _valid_body() + " Late fee of Rs.50 may apply."
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid
    assert any("50" in e for e in result.errors)


def test_prohibited_legal_threat_is_rejected() -> None:
    body = _valid_body() + " Failure to pay may result in legal action."
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid
    assert any("prohibited" in e for e in result.errors)


def test_prohibited_manufactured_urgency_is_rejected() -> None:
    body = "URGENT: " + _valid_body()
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid
    assert any("prohibited" in e for e in result.errors)


def test_prohibited_credit_score_threat_is_rejected() -> None:
    body = _valid_body() + " This may affect your CIBIL score."
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid


def test_exceeding_channel_length_cap_is_rejected() -> None:
    body = _valid_body() + " " * 400
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid
    assert any("length cap" in e for e in result.errors)


def test_email_channel_has_a_higher_length_cap_than_sms() -> None:
    body = _valid_body() + " " * 400  # exceeds sms cap (320), not email cap (2000)
    sms_result = validate_notice(
        body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED
    )
    email_result = validate_notice(
        body, channel="email", whitelist=WHITELIST, required_present=REQUIRED
    )
    assert not sms_result.valid
    assert email_result.valid


def test_multiple_simultaneous_violations_are_all_reported() -> None:
    body = "URGENT: contact our legal team. Late fee Rs.999 may apply."
    result = validate_notice(body, channel="sms", whitelist=WHITELIST, required_present=REQUIRED)
    assert not result.valid
    assert len(result.errors) >= 4  # missing fields, opt-out, ungrounded number, prohibited x2
