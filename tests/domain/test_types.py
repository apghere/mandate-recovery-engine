from app.domain.types import CAUSE_DISPOSITION, Cause, DenyReason, Verdict


def test_every_cause_has_a_disposition() -> None:
    assert set(CAUSE_DISPOSITION.keys()) == set(Cause)


def test_verdict_allow_carries_no_reason() -> None:
    v = Verdict.allow()
    assert v.allowed is True
    assert v.reason_code is None


def test_verdict_deny_requires_reason() -> None:
    v = Verdict.deny(DenyReason.OPTED_OUT)
    assert v.allowed is False
    assert v.reason_code is DenyReason.OPTED_OUT


def test_verdict_rejects_inconsistent_construction() -> None:
    import pytest

    with pytest.raises(ValueError):
        Verdict(allowed=True, reason_code=DenyReason.OPTED_OUT)
    with pytest.raises(ValueError):
        Verdict(allowed=False, reason_code=None)
