"""RBI-compliant notice generation behind the deterministic validator
(docs K.5). The LLM drafts; `validate_notice` decides. On rejection: one
repair attempt with the validator's own errors attached, then a hard
fallback to a static, always-valid template — an unvalidated LLM output
never reaches a payer, and this is the module that guarantees it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.ai.client import LlmUnavailable, complete
from app.ai.validator import MAX_LENGTH_BY_CHANNEL, ValidationResult, validate_notice

_NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class NoticeVariables:
    merchant_name: str
    amount: str  # pre-formatted, e.g. "Rs.500"
    debit_date: str  # pre-formatted, e.g. "15 September 2026"
    debit_time: str  # e.g. "02:00"
    mandate_ref: str
    reason: str
    channel: str = "sms"


@dataclass(frozen=True)
class NoticeResult:
    body: str
    generated_by: str  # "llm" | "template"
    validator_result: ValidationResult
    repaired: bool = False


_SYSTEM_PROMPT_TEMPLATE = """You draft a short, compliant notification telling a \
payer that a recurring payment will be attempted. Rules, all mandatory:

- Use ONLY the facts given to you. Never invent, guess, or round any number, \
date, or name.
- Must literally include: the merchant name, the amount, the debit date, the \
debit time, the mandate reference, and the reason for the debit.
- Must include a clear opt-out instruction (e.g. "Reply STOP to opt out").
- No threats, no mention of legal action, credit score, police, or any \
consequence beyond the debit itself.
- No manufactured urgency ("urgent", "immediately", "last chance", "final \
notice").
- Plain, calm, factual tone. Under {max_len} characters.

Respond with ONLY the notice text, nothing else — no preamble, no quotes."""


def _static_template(v: NoticeVariables) -> str:
    return (
        f"{v.merchant_name}: a payment of {v.amount} on mandate {v.mandate_ref} "
        f"will be attempted on {v.debit_date} at {v.debit_time} ({v.reason}). "
        f"Reply STOP to opt out."
    )


def _whitelist_for(v: NoticeVariables) -> set[str]:
    tokens: set[str] = set()
    for field_value in (v.amount, v.debit_date, v.debit_time, v.mandate_ref):
        tokens.update(_NUMBER_PATTERN.findall(field_value))
    return tokens


def _required_fields(v: NoticeVariables) -> dict[str, str]:
    return {
        "merchant_name": v.merchant_name,
        "amount": v.amount,
        "debit_date": v.debit_date,
        "debit_time": v.debit_time,
        "mandate_ref": v.mandate_ref,
        "reason": v.reason,
    }


def _draft_with_llm(v: NoticeVariables, *, repair_note: str | None = None) -> str | None:
    max_len = MAX_LENGTH_BY_CHANNEL.get(v.channel, 320)
    system = _SYSTEM_PROMPT_TEMPLATE.format(max_len=max_len)
    user = (
        f"merchant_name: {v.merchant_name}\n"
        f"amount: {v.amount}\n"
        f"debit_date: {v.debit_date}\n"
        f"debit_time: {v.debit_time}\n"
        f"mandate_ref: {v.mandate_ref}\n"
        f"reason: {v.reason}"
    )
    if repair_note:
        user += f"\n\nYour previous draft was rejected for: {repair_note}. Fix it."
    try:
        response = complete(system=system, user=user, max_tokens=300)
    except LlmUnavailable:
        return None
    return response.text.strip()


def generate_notice(v: NoticeVariables) -> NoticeResult:
    whitelist = _whitelist_for(v)
    required = _required_fields(v)

    draft = _draft_with_llm(v)
    if draft is not None:
        result = validate_notice(
            draft, channel=v.channel, whitelist=whitelist, required_present=required
        )
        if result.valid:
            return NoticeResult(body=draft, generated_by="llm", validator_result=result)

        repair_note = "; ".join(result.errors)
        repaired_draft = _draft_with_llm(v, repair_note=repair_note)
        if repaired_draft is not None:
            repaired_result = validate_notice(
                repaired_draft, channel=v.channel, whitelist=whitelist, required_present=required
            )
            if repaired_result.valid:
                return NoticeResult(
                    body=repaired_draft,
                    generated_by="llm",
                    validator_result=repaired_result,
                    repaired=True,
                )

    fallback_body = _static_template(v)
    fallback_result = validate_notice(
        fallback_body, channel=v.channel, whitelist=whitelist, required_present=required
    )
    return NoticeResult(
        body=fallback_body, generated_by="template", validator_result=fallback_result
    )
