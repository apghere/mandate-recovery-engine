# Razorpay test-mode findings

Spike date: 2026-08-26. Hard cap: 4 hours (docs §N Day 1).

Purpose: determine empirically what the real Razorpay test-mode surface can
drive, so we know whether/where MRE can integrate with it versus needing to
rely entirely on `simulator/` (which is designed to need zero external
dependency regardless of the outcome here).

## 1. Account + API keys

- [ ] Test-mode account created
- [ ] Test `key_id` / `key_secret` generated (stored in `.env`, never committed)

## 2. Subscriptions (Plan -> Subscription -> mandate authorization)

- [ ] Could create a Plan
- [ ] Could create a Subscription
- [ ] Could authorize it via test UPI / test card
- [ ] "Charge this now" button: what state/webhook did it actually produce?

Notes:

## 3. Forcing specific failure outcomes

- [ ] `success@razorpay` / `failure@razorpay` confirmed
- [ ] Amount-in-paise -> specific UPI error code mapping (paste the table from
      Razorpay's UPI Error Codes doc here)
- [ ] Confirmed: UPI *cancellation* succeeds instead of failing in test mode
      (per docs) — re-verify

Notes:

## 4. Webhooks

- [ ] Test-mode webhook URL registered (separate from Live)
- [ ] Delivery mechanism used to receive locally (ngrok / webhook.site / other)
- [ ] Events actually observed:
- [ ] Webhook secret captured (stored in `.env`, never committed)

Raw payload samples (redact anything sensitive):

```json
```

## 5. Payment Links

- [ ] Could create and pay a test-mode Payment Link end to end

## 6. UPI Autopay / eNACH mandate specifics

- [ ] Anything found about NPCI's 4-attempt cap or execution windows being
      simulable in test mode? (Expectation going in: no — this is why
      `simulator/` exists as the independent rail.)

## 7. Verdict

Zero-external-dependency architecture confirmed / not needed? What, if
anything, changes in the plan as a result of this spike.
