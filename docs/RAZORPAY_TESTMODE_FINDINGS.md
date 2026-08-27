# Razorpay test-mode findings

Spike date: 2026-08-26. Hard cap: 4 hours (docs §N Day 1).

Purpose: determine empirically what the real Razorpay test-mode surface can
drive, so we know whether/where MRE can integrate with it versus needing to
rely entirely on `simulator/` (which is designed to need zero external
dependency regardless of the outcome here).

## 1. Account + API keys

- [x] Test-mode account created and confirmed active (Dashboard shows TEST mode)
- [x] Test `key_id` / `key_secret` generated, stored in `.env` (gitignored),
      plaintext CSV export deleted from disk after transcription

## 2. Subscriptions (Plan -> Subscription -> mandate authorization)

- [x] Could create a Plan — `plan_TUUevfrBveckj` ("Test Recurring Plan",
      ₹500, every 2 months, qty 1)
- [x] Could create a Subscription — `sub_TUUyRmF15TET3`, 3 cycles, immediate
      start, end date 03 Jan 2027. Link: `https://rzp.io/rzp/0ooZZQAn`
- [ ] Could authorize it via UPI Autopay — **blocked, see §6**
- [ ] "Charge this now": not yet exercised (waiting on a UPI-authorized
      subscription so we're testing the right rail — see "Important testing
      decision" below)

Notes: Checkout on the subscription link offered **Cards** and **eMandate**
only. **UPI Autopay did not appear as an option.** We deliberately did not
complete authorization via eMandate or card, since that would validate the
wrong payment rail (target is specifically UPI Autopay, which is the
dominant/high-failure-rate rail per docs §A.3 — 8-15% failure vs 2-3% for
cards). `sub_TUUyRmF15TET3` is being kept only as a lifecycle/webhook test
resource, not as proof of UPI Autopay support.

## 3. Forcing specific failure outcomes

- [ ] Not yet exercised — depends on having a UPI-authorized subscription to
      run "Charge this now" against
- [ ] Amount-in-paise -> specific UPI error code mapping — not yet pulled
      (docs reference a table on Razorpay's "UPI Error Codes" page)
- [ ] UPI cancellation-succeeds-in-test-mode quirk — not yet re-verified

## 4. Webhooks

Deferred — see "Root cause identified" below. Sequenced as parallel work
alongside the UPI Autopay support ticket (webhook receiver doesn't depend on
which payment rail eventually authorizes).

## 5. Payment Links

Not yet exercised.

## 6. UPI Autopay / eNACH mandate specifics — BLOCKER

**Root cause:** Subscriptions → Settings → Payment Methods shows Card and
eMandate as Enabled, but UPI Autopay is explicitly **not enabled** on this
test-mode account:

> "UPI autopay as a payment method is not enabled, please click below to
> raise a request."

Clicking **Enable UPI autopay** redirects to the Dashboard home without
visibly creating or confirming a request (reproduced more than once, same
behavior each time).

**Action taken:** Razorpay Support ticket raised (2026-08-2x — record exact
date when confirmed) requesting UPI Autopay enablement for this test-mode
account, explicitly describing the redirect-without-confirmation bug.

**Support's response (2026-08-27):** full KYC/account verification is
required before UPI Autopay can be enabled, even in test mode; ETA
~24-48h after KYC completes (not guaranteed).

**Strategic decision (2026-08-27):** submit KYC today as a cheap,
non-blocking background task (confirm the lightest applicable document
tier — Individual, not a registered business — and that test-mode KYC
carries no live-mode obligations, *before* submitting), then allocate
**zero engineering hours to waiting on it**. Full rationale below.

Why this is safe rather than a compromise: none of the project's P0
MUST-HAVE features (§G.2 M1-M13) touch live UPI Autopay execution, and
critically, **Razorpay's sandbox was never going to let us observe the
NPCI 4-attempt cap, execution windows, or notice timing regardless of UPI
Autopay status** — nothing suggests their test mode enforces or exposes
those mechanics. That evidence source was always `simulator/`, by design
(docs §H.2). Breaking down what actually needs live Razorpay evidence:

| Needs proving via | Real Razorpay evidence required? |
|---|---|
| Subscription/mandate creation mechanics | No — already have it (`sub_TUUyRmF15TET3`), rail-agnostic |
| Webhook delivery + HMAC authenticity | No — identical regardless of triggering rail; capturable today via eMandate/Card |
| Recurring charge mechanics ("Charge this now") | No — same reasoning |
| Success/failure outcomes | Simulated by design |
| Retry/stopping-rule/NPCI-cap/notice-timing mechanics | Simulated only, always — never observable via Razorpay's sandbox either way |
| State transitions, idempotency, recovery | Proven by our own test/chaos suite |
| UPI Autopay-specific checkout UI appearing | **Yes — the one genuinely gated item** |

One item out of seven categories is actually blocked, and it's cosmetic
(demo realism), not evidentiary.

**If UPI Autopay unblocks later this week:** time-boxed 2-4h capture
(matching the original Day-1 spike discipline), priority order: (1)
screenshot/clip of UPI appearing in checkout — the one genuinely new fact;
(2) one successful `success@razorpay` authorization; (3) "Charge this now"
+ captured webhook against the newly-UPI-authorized subscription; (4) if
time remains, `failure@razorpay` + paise-amount-coded error scenarios
(could feed real-world-grounded strings back into `data/taxonomy.yaml`).
Stop at the time-box regardless of progress.

**If it never unblocks:** no impact on the shippable project. Add to
README Limitations (§S.1) as a named, honestly-framed gap — same energy as
§F.1's "this is a simulator I wrote" honesty stance, not a failure to
hide.

**What we will NOT do as a result of this:** block Phase 3+ on KYC/Support
turnaround; substitute eMandate for UPI Autopay in any claim or demo
footage; write `RazorpayRailClient` UPI-integration code before it's
actually enabled and testable (untested integration code against an
unverified surface is a liability, not progress — see §4 below); overstate
UPI Autopay testing in the README/video if it doesn't happen.

**Explicit decision:** we are NOT substituting eMandate for UPI Autopay.
They are different rails with different failure/retry characteristics (docs
§A.3's NPCI four-attempt cap and window rules are UPI Autopay-specific); an
eMandate-based demo would not validate the target claim. Do not assume
`sub_TUUyRmF15TET3` becomes UPI-capable retroactively once enabled — plan to
create a fresh subscription post-enablement and confirm UPI actually appears
in checkout before treating it as validated.

## 7. Verdict (interim — pending Support resolution)

**Zero-external-dependency architecture is validated, not just confirmed as
a fallback.** UPI Autopay isn't even reliably reachable in Razorpay's own
test-mode sandbox without a support-gated enablement step, which by itself
justifies §H.2's decision to make `simulator/` the independently-enforcing
mandate rail rather than depending on live Razorpay behavior for anything
correctness-critical. Real Razorpay test mode, once unblocked, remains
valuable for: validating realistic webhook payload shapes, and (P1,
Day 6 SHOULD-HAVE only) the escalation-ladder Payment Link. It is not, and
was never meant to be, on the critical path for the NPCI-cap / retry-window
mechanics that are the actual thesis of the project.

Proven so far: Test Mode -> Plan creation -> Subscription creation ->
Subscription ID generation -> Checkout access -> payment-method
configuration discovery (which is what surfaced the blocker). Not blocked
overall — see engineering log for what's proceeding in parallel.
