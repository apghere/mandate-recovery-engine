# Signal legitimacy — what this system can actually know

Written in response to a direct challenge to the project's core
assumption: *how do we know when a user will have money?* The honest
answer is we don't, we never claimed to, and the architecture doesn't
need to. This doc exists so that answer is on record, not just said once
in a conversation — the same "the evidence layer cannot be faked" ethos
that governs everything else in this repo (docs §F point 4).

## The one-sentence version

**The model predicts P(this specific debit succeeds at this specific
time), learned from this payer's and this issuer's own historical
outcomes — never "does the user have money," "where is the user's
money," or "when does the user get paid."** Those three would require
data we don't have and, for the last one, data that doesn't exist yet
(the future). The one we actually predict requires only data our own
platform already generates.

## Observability table

Classification is one of three states, no hedging:
**VERIFIED** (we have this, no extra integration needed) ·
**POSSIBLE WITH EXPLICIT CONSENT / ADDITIONAL INTEGRATION** (real
mechanism exists, out of scope for this build) ·
**NOT AVAILABLE** (no legitimate mechanism, full stop).

| Signal | Status | Mechanism | MVP-suitable |
|---|---|---|---|
| Previous successful debit timestamps | VERIFIED | Razorpay's own Subscriptions/mandate records | Yes |
| Previous failed debit timestamps + raw decline strings | VERIFIED | Razorpay's published error taxonomy (docs §A.4) | Yes |
| Aggregated historical payment behaviour | VERIFIED | Derived from the above, on our own platform | Yes |
| Mandate execution outcomes | VERIFIED | Core Subscriptions data | Yes |
| Payment-method / issuer metadata | VERIFIED | Known at mandate creation | Yes |
| Payment Downtime webhooks | VERIFIED | Razorpay's published API (docs §A.4) | Yes |
| Bank account balance | **NOT AVAILABLE** | A payment aggregator has no live balance visibility via UPI/eNACH rails — that's the bank's domain | No |
| Full bank/UPI transaction history, incoming credits, salary credits | **POSSIBLE WITH CONSENT** | India's Account Aggregator framework (RBI-regulated, via an NBFC-AA such as Setu/Finvu/CAMS) — a real, separate, consent-driven integration, never bundled with a payment-aggregator relationship by default | No — correctly out of scope for a 7-day build |
| GPay balance / Slice balance / other bank accounts | **NOT AVAILABLE** | No mechanism exists for a third-party payment aggregator to read another provider's wallet balance | No |
| Cash | Not observable by any digital system, by anyone | — | No |
| Future income ("will money arrive") | **NOT KNOWABLE** — this is not a data-access gap, it is the future | — | No, and nothing in this system claims otherwise |

Everything the model actually uses (see `app/ml/features.py`'s
`FEATURE_NAMES`) lives in the top block. Nothing requires the bottom
block. This isn't an accident — it's the reason the target was chosen as
a success probability instead of an income predictor.

## Why this framing survives the two hardest real-world cases

**"Money moved elsewhere" (e.g. salary lands, gets swept into GPay/another
account, the mandate's own debit source stays thin).** A model asked to
predict *income arrival* would be fooled by this every time. A model
predicting *debit success*, trained on real outcome labels, isn't fooled
at all — it simply learns a dampened pattern directly from what actually
happened: "day 1-3 has only mildly elevated success, because for a
meaningful share of payers the debit source isn't where the salary
lands." No special-casing required. The model never needed to know
*where* the money went, because it was never asked to track money — only
to correlate timing with the one thing it can actually observe, the
debit outcome.

**Irregular / unpredictable liquidity (e.g. a student relying on
ad-hoc transfers).** The instinct here is to build an explicit
`Segment A / B / C` predictability classifier. We didn't, on purpose —
`Payer.balance_volatility` (`data/generator.py`) already does this as a
continuous, learned signal rather than a hand-coded bucket, and it's
already tested:

```
predictable payer (volatility 0.2), P(success) at day-since-credit 0/14/27: 0.99, 0.96, 0.01
irregular   payer (volatility 1.4), P(success) at day-since-credit 0/14/27: 0.94, 0.61, 0.16
```

(`simulator/decline.py::funds_sufficiency_probability`, exercised by
`tests/simulator/test_decline.py::test_higher_volatility_flattens_the_curve`,
and reproducible on demand via `scripts/demo_predictability.py`.)

The predictable payer's curve is sharp — timing clearly matters, and the
planner (`domain/planner.py`) will correctly wait for the good slot. The
irregular payer's curve is flat — no single slot is dramatically better,
so the planner's optimal-stopping math naturally becomes more
conservative about spending attempts on blind retries and escalates
sooner. That's the exact behaviour a hard-coded segment classifier would
be trying to hand-engineer, produced for free by a continuous input and
an expected-value calculation. Adding an explicit segmentation layer on
top would be redundant complexity, not an improvement.

## If this were deployed against real users

The only feature that would need to change how it's sourced is the
credit-day-derived timing signal (see `docs/DATA_MODEL.md`'s `credit_day`
section) — every other feature already reads (in this demo) or would
read (in a real deployment) from data Razorpay's own platform legitimately
has. That's a data-plumbing change (compute a per-payer empirical
successful-debit-day pattern from real history instead of a synthetic
generator), not a modelling change and not an architecture change.
