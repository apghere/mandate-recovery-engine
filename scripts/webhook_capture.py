"""Throwaway spike tool: capture real Razorpay webhook payloads.

Not part of the production ingestion path (that's Phase 3's
backend/app/api — idempotent, wired to the event pipeline). This exists
only to let us look at real payload shapes and verify HMAC signature
mechanics *today*, using whichever rail is already authorized (Card /
eMandate), without waiting on the UPI Autopay KYC block — see
docs/RAZORPAY_TESTMODE_FINDINGS.md 6-7. Webhook delivery + signature
verification are identical regardless of which payment rail triggered the
event, so this evidence is real and rail-agnostic.

Usage:
    export RAZORPAY_WEBHOOK_SECRET=<from Dashboard -> Settings -> Webhooks>
    .venv/bin/uvicorn scripts.webhook_capture:app --port 8000
    ngrok http 8000
    # register the ngrok https URL + /webhook as the TEST-mode webhook URL
    # in the Razorpay Dashboard, subscribe to the events listed in
    # docs/RAZORPAY_TESTMODE_FINDINGS.md, then trigger some (e.g. via
    # "Charge this now" on the existing subscription).

Captured payloads land in data/generated/razorpay_webhook_captures.jsonl
(gitignored — copy the 2-3 best ones into the findings doc by hand, since
they may contain real-looking identifiers you want to review before
committing).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request

_REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_PATH = _REPO_ROOT / "data" / "generated" / "razorpay_webhook_captures.jsonl"

app = FastAPI(title="Razorpay webhook capture (spike tool)")


def _verify_signature(raw_body: bytes, signature: str | None, secret: str | None) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, bool]:
    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature")
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    verified = _verify_signature(raw_body, signature, secret)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        payload = {"_raw_unparseable": raw_body.decode("utf-8", errors="replace")}

    record = {
        "captured_at": datetime.now(UTC).isoformat(),
        "event": payload.get("event"),
        "signature_present": signature is not None,
        "signature_verified": verified,
        "payload": payload,
    }

    CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CAPTURE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")

    print(f"[webhook_capture] event={record['event']} verified={verified}")
    return {"received": True}
