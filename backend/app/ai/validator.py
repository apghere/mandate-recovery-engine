"""Deterministic NoticeValidator (docs K.5). The LLM drafts; this module
decides — every check here is pure, needs no LLM call, and is directly
unit-testable, which is what makes "policy-violation rate 0.00%,
asserted in CI" (docs I.15) a claim we can actually check rather than
hope for.

Scope, stated plainly rather than left implicit: the whitelist-grounding
check (K.5 point 2, "every number, date and proper noun... must appear
in the supplied variable set") is implemented here for **numbers** —
the highest-stakes hallucination target (a wrong amount or a wrong
reference number is the actual compliance risk RBI's rule exists to
prevent). Broader free-text proper-noun grounding (catching, say, a
hallucinated *second* merchant name) would need real NER to do without
false-positives on ordinary capitalized English words, which isn't worth
building for a 7-day MVP; the merchant name itself is still hard-checked
via the required-fields list below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

REQUIRED_FIELD_KEYS = (
    "merchant_name",
    "amount",
    "debit_date",
    "debit_time",
    "mandate_ref",
    "reason",
)

MAX_LENGTH_BY_CHANNEL: dict[str, int] = {"sms": 320, "email": 2000, "push": 200}

OPT_OUT_MARKERS = ("stop", "opt-out", "opt out", "unsubscribe")

# Threats, fabricated legal/credit consequences, manufactured urgency —
# docs K.5 point 3. Not exhaustive; the point is a real, checkable floor,
# not a claim of completeness.
PROHIBITED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\blegal action\b", re.IGNORECASE),
    re.compile(r"\bcourt\b", re.IGNORECASE),
    re.compile(r"\bpolice\b", re.IGNORECASE),
    re.compile(r"\bcibil\b", re.IGNORECASE),
    re.compile(r"\bcredit score\b", re.IGNORECASE),
    re.compile(r"\burgent(ly)?\b", re.IGNORECASE),
    re.compile(r"\bimmediately\b", re.IGNORECASE),
    re.compile(r"\blast chance\b", re.IGNORECASE),
    re.compile(r"\bfinal (notice|warning)\b", re.IGNORECASE),
)

_NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


def _numbers_in(text: str) -> set[str]:
    return set(_NUMBER_PATTERN.findall(text))


def validate_notice(
    body: str,
    *,
    channel: str,
    whitelist: set[str],
    required_present: dict[str, str],
) -> ValidationResult:
    errors: list[str] = []

    for key in REQUIRED_FIELD_KEYS:
        value = required_present.get(key)
        if not value or value not in body:
            errors.append(f"missing required field: {key}")

    if not any(marker in body.lower() for marker in OPT_OUT_MARKERS):
        errors.append("missing opt-out instruction")

    for number in _numbers_in(body):
        if number not in whitelist:
            errors.append(f"ungrounded number not in whitelist: {number!r}")

    for pattern in PROHIBITED_PATTERNS:
        if pattern.search(body):
            errors.append(f"prohibited content matched: {pattern.pattern!r}")

    max_len = MAX_LENGTH_BY_CHANNEL.get(channel)
    if max_len is not None and len(body) > max_len:
        errors.append(f"exceeds {channel} length cap of {max_len} (got {len(body)})")

    return ValidationResult(valid=not errors, errors=errors)
