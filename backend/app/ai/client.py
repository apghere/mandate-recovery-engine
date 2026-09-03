"""Anthropic API client wrapper — the one place the SDK is touched, for
both the decline-string normaliser (docs K.2) and the notice generator
(docs K.5). Both are narrow, off-the-hot-path LLM uses with deterministic
fallbacks; this module's whole job is making "the LLM is unavailable" a
clean, expected outcome rather than an exception someone forgot to catch.

Model: Claude Haiku 4.5 — an explicit, documented choice from the source
plan (docs K.2), not a cost-driven default (see docs/ADR/0004).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import anthropic

from app.config import get_settings

MODEL = "claude-haiku-4-5"
TIMEOUT_SECONDS = 4.0  # docs M.1 failure matrix: "4s timeout, 2 retries with jitter"
MAX_RETRIES = 2


class LlmUnavailable(Exception):
    """No API key configured, or the call failed after retries (timeout,
    rate limit, connection error, malformed response). Callers must treat
    this as "fall back to the deterministic path" — docs M.1's designed-
    for degraded mode, never a hard error."""


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is not None:
        return _client
    api_key = get_settings().anthropic_api_key
    if not api_key:
        raise LlmUnavailable("ANTHROPIC_API_KEY is not set")
    _client = anthropic.Anthropic(
        api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES
    )
    return _client


@dataclass(frozen=True)
class LlmTextResponse:
    text: str
    model: str


def complete(*, system: str, user: str, max_tokens: int = 512) -> LlmTextResponse:
    """One-shot text completion. Raises LlmUnavailable on any failure at
    all — missing credentials, timeout, rate limit, connection error, or a
    response with no text block. Callers own the fallback; this function
    never returns a partial or best-effort result."""
    client = get_client()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AnthropicError as exc:
        raise LlmUnavailable(str(exc)) from exc

    for block in response.content:
        if block.type == "text":
            return LlmTextResponse(text=block.text, model=response.model)
    raise LlmUnavailable("response contained no text block")


def input_hash(*parts: str) -> str:
    """Deterministic cache key — docs K.2: normalisation is "cached by
    hash(raw_string)"."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
