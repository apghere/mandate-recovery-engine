# Mandate Recovery Engine

"You get four attempts. Spend them well."

**Start reading at [`backend/app/domain/planner.py`](backend/app/domain/planner.py)** — the pure,
~160-line backward-induction solver that is the actual product. Everything else exists to feed it
real numbers and execute what it decides.

Built for the Razorpay AI Buildathon 2026, Track 03 (AI Revenue Recovery).

---

## 1. Problem

Indian recurring payments (UPI AutoPay, eNACH) fail at 8–15%, against 2–3% for card mandates.
RBI's Digital Payments E-mandate Framework 2026 caps recovery at **four attempts**, inside
**NPCI-permitted execution windows**, gated by a **mandatory 24-hour pre-debit notice**. Miss the
window, skip the notice, or burn the fourth attempt on a bad guess, and the mandate is gone —
not delayed, gone. Most merchants respond with a fixed D+1/D+3/D+7 retry schedule copied from
American dunning playbooks that assume unlimited retries and no notice requirement. Neither
assumption holds here.

## 2. Why it matters

Over 20 million UPI AutoPay mandates are revoked every month because a payer's balance came up
short at the wrong moment — not because they don't want to pay, but because nobody asked at the
right time. The loss on a failed cycle isn't that cycle's amount; it's the mandate's entire
remaining lifetime value, since a revoked mandate has to be re-authorised from scratch.

## 3. Solution

A constrained retry-budget planner: given a decline, canonicalise the cause, forecast a
calibrated P(success) at every legally permitted slot over a 14-day horizon, then solve — exactly,
by backward induction, not heuristically — for the sequence of at most four attempts and their
required notices that maximises expected recovered value, **with an explicit stopping rule**: stop
and escalate to manual recovery the moment continuing is worth less than escalating now. Every
scheduled action is re-authorised independently at execution time by a pure policy engine, so a
planning mistake can never become an unauthorised debit.

## 4. Why AI — and where it deliberately is not

Two narrow, bounded LLM uses, both off the money path:

- **Decline-reason normaliser** ([`app/ai/normalizer.py`](backend/app/ai/normalizer.py)) — dictionary
  → fuzzy match → Claude Haiku 4.5 → `UNKNOWN`. Free-text bank remarks are unbounded; the output
  space (13 canonical causes) is not. The LLM's job is compressing the tail the dictionary
  doesn't cover — not deciding what happens next.
- **Notice generator** ([`app/ai/notice.py`](backend/app/ai/notice.py)) — the LLM drafts,
  a deterministic validator decides (RBI-required fields present, every number/date grounded in a
  literal whitelist, no manufactured urgency, per-channel length caps). One repair attempt, then a
  hard fallback to a static, self-consistent template.

**Retry timing, attempt count, money movement, constraint enforcement, the stopping decision, and
escalation triggering are never AI.** The success-probability model (`HistGradientBoostingClassifier`
+ isotonic calibration) is the third machine-learned component, and it is deliberately not an LLM —
LLMs are badly calibrated probability estimators, and a planner that consumes probabilities needs
calibration, not vibes. The planner itself is exact backward induction over ~280 states, solved in
sub-millisecond time — deterministic, explainable, and reproducible in a way an LLM call is not.

Prompt-injection posture, stated plainly rather than hedged: *the normaliser was not made
injection-proof — its output space was made too small for injection to matter.* The worst a
successful injection can do is one misclassification into a 13-value enum, and the policy engine
bounds what any misclassification can do downstream. `tests/ai/test_normalizer.py` red-team-tests
this against every adversarial string in `data/taxonomy.yaml`, simulating an LLM that *is*
successfully injected, to prove the bound holds even then.

## 5. Architecture

```mermaid
flowchart TB
    subgraph client["Merchant / Payer"]
        webhook[Webhook events]
    end

    subgraph api["FastAPI — api/app.py + api/dashboard.py"]
        events["/events — idempotent ingestion, HMAC verified"]
        cases["/cases, /metrics, /audit, /admin/kill-switches"]
    end

    subgraph domain["Domain core — pure, no I/O, no clock, no network"]
        normalizer["ai/normalizer.py — cause resolution + abstention"]
        scorer["ml/inference.py — calibrated P(success | slot)"]
        planner["domain/planner.py — exact DP over (slot, budget, notice)"]
        policy["domain/policy.py — authorize() → Allow | Deny(reason_code)"]
        fsm["domain/fsm.py — recovery-case state machine"]
    end

    subgraph pg[("PostgreSQL 16")]
        tables["events · mandates · cycles · attempt_intents · plans ·
plan_steps · notifications · decisions · audit_ledger · outbox"]
    end

    subgraph worker["worker.py — single process"]
        outbox["FOR UPDATE SKIP LOCKED
due plan_steps + outbox drain"]
    end

    subgraph sim["simulator/ — separate service, own SQLite"]
        rail["independently enforces the NPCI
4-attempt cap + permitted windows"]
    end

    subgraph dash["frontend/*.html — Tailwind, no build step"]
        d1[Case detail + counterfactual]
        d2[Benchmark — evaluation/runner.py]
        d3[Audit — chain validity + kill switch]
    end

    webhook --> events --> domain
    domain --> pg
    pg --> worker
    worker <--> sim
    dash --> api
    api --> pg
```

Component justification for the two non-obvious choices: **Postgres**, not SQLite or Mongo,
because the four-attempt guarantee must be a database constraint
(`UNIQUE(cycle_id, sequence_no)`), not application logic — without it, the guarantee is a hope.
**A single worker on a Postgres queue** (`SELECT ... FOR UPDATE SKIP LOCKED`), not Celery+Redis,
because planning and execution must be asynchronous without introducing a consistency boundary
between "decided" and "enqueued" — the same transaction that reserves an attempt also enqueues its
delivery, so a crash between the two is impossible by construction, not by retry logic.

## 6. Key workflows

- **W1 — Automatic recovery.** Cycle fails → cause normalised → slots scored → plan solved (e.g.
  notice D+1, attempt D+2, notice D+5, attempt D+6, stop) → each step re-authorised independently
  at execution time → an attempt succeeds → remaining steps cancelled → case sealed.
- **W2 — Stop and escalate.** Cause looks unrecoverable (e.g. mandate revoked) → the planner values
  every continuation near zero → the stopping rule fires at the DP's own root comparison → **zero
  attempts consumed** → `AWAITING_MANUAL`.
- **W3 — Compliance-blocked execution.** The plan wants a debit at D+2, but the D+1 notice never
  actually sent. The policy engine denies at execution time with `RBI_NOTICE_NOT_SATISFIED` —
  independently of what the plan assumed — the attempt is **not consumed**, and the case stays
  `SCHEDULED` for its next pre-planned step. This is the single most important correctness property
  in the system: a planning mistake never becomes an unauthorised debit.
- **W4 — Operator review.** The case-detail dashboard screen shows the plan, every slot's
  probability, every denied action with its reason code, the fixed-schedule counterfactual, and the
  full hash-chained ledger.

## 7. Safety model

Three independent layers protect the four-attempt cap, deliberately redundant so a bug in one
never becomes an unauthorised debit:

1. **Database constraint** — `attempt_intents` has `UNIQUE(cycle_id, sequence_no)` with
   `sequence_no BETWEEN 1 AND 4`. Not application logic; Postgres itself rejects a fifth row.
2. **Policy re-check at execution time** — `domain/policy.py`'s `authorize()` independently
   re-verifies attempt budget, execution window, notice freshness (≥24h, ≤7d, covering *this*
   debit), mandate status, opt-out flag, AFA threshold, and both kill switches — every single time,
   regardless of what the plan assumed when it was built.
3. **The simulator rejects independently** — a separate FastAPI service with its own SQLite store,
   coded independently of the main app, so a bug in one can never silently pass the other
   (`tests/simulator/test_simulator.py::test_store_rejects_a_fifth_attempt_even_bypassing_the_api`).

Idempotency is layered the same way: `events.external_id` is unique at the database level, outbox
delivery is keyed by idempotency key, and the simulator deduplicates on that same key — a
duplicate webhook produces exactly one effect no matter which layer catches it first.

## 8. Evaluation

The rigorous, paired benchmark lives in [`evaluation/runner.py`](evaluation/runner.py) — every
policy (`fixed`, `greedy`, `mre`, and an `oracle` perfect-information ceiling) runs against the
**same** batch of payers in a **shared realised world** (outcomes are seeded by
`(payer_id, scheduled_for, sequence_no)`, not by which policy is asking), with a nonparametric
paired bootstrap 95% CI on the rupee gap between every pair. The locked run, on the sealed test
split (500 payers, touched exactly once):

| policy | recovered | rate | rupees | attempts |
|---|---:|---:|---:|---:|
| fixed | 481 | 96.2% | 415,700.10 | 685 |
| greedy | 473 | 94.6% | 410,329.34 | 676 |
| mre | 474 | 94.8% | 412,213.85 | 685 |
| oracle | 475 | 95.0% | 411,238.50 | 682 |

Full report, generated by script (not by hand): [`reports/BENCHMARK.md`](reports/BENCHMARK.md).

**The honesty paragraph.** This is not the result we expected or would have chosen to report.
`fixed` — a policy with no stopping rule, no timing intelligence, and no cost model at all —
recovered *significantly more* gross rupees than MRE, greedy, and even the oracle ceiling
(all three `fixed`-vs-X comparisons have 95% CIs that exclude zero). We investigated rather than
hid this: it is not an artifact of ignoring MRE's escalation cost (checked directly — escalations
are rare, 2/500, and cheap relative to a ~₹864 average recovery). The real cause is that MRE's
planner prices `optout_hazard_cost`/`revoke_hazard_cost` — real constants representing "an extra
low-odds attempt risks annoying the payer into revoking the mandate" — into every decision, so it
rationally skips a handful of low-probability attempts that `fixed`'s blind schedule takes anyway
and occasionally wins on. Neither the simulator nor this benchmark's single-cycle metric can
observe or credit the benefit that trade-off is supposed to buy: there is no stochastic
revocation/opt-out model tied to contact frequency (a documented, prior scope cut — see
Limitations below), and a single cycle can't show a payer who didn't churn later from being
contacted less. On the earlier, larger `dev`-split runs the direction was reversed and modest
(MRE ~1.4% ahead of `fixed`, significant against `greedy` specifically) — the locked test-split
result did not confirm it. **Whatever the numbers say, they are the numbers.** The full
investigation, including the dev-split runs, the E_MANUAL sensitivity sweep (robust across
{100, 150, 250}), and the exact SQL used to rule out the first hypothesis, is in `CLAUDE.md`'s
Phase 8 notes.

**P0b — the deterministic-lookup-table baseline** (docs §T red-team item 2, "a deterministic
lookup table could replace the ML model — ship it as a fifth baseline and report it"). Added after
the sealed test split was already spent, so this comparison runs on `dev` only, by the same
touch-once discipline that governs the locked result above — not a second use of `test`.
`app/ml/lookup_baseline.py` fits a plain `(cause, day-of-month)` success-rate table from the exact
same `train`-split corpus the GBM trains on, with a documented small-sample backoff (bucket →
cause → global); `evaluation.runner`'s `lookup` policy feeds that table into the identical
naive-greedy scheduler `greedy` uses, so the *only* thing it isolates relative to `greedy` is
whether the calibrated model beats a plain table — the DP-vs-lookahead question is a separate axis
(`mre` vs `greedy`). `dev`, n=500:

| policy | recovered | rate | rupees | attempts |
|---|---:|---:|---:|---:|
| fixed | 473 | 94.6% | 493,824.01 | 670 |
| greedy | 467 | 93.4% | 491,645.96 | 672 |
| lookup | 469 | 93.8% | 493,814.67 | 672 |
| mre | 476 | 95.2% | 500,562.70 | 682 |
| oracle | 476 | 95.2% | 498,514.74 | 681 |

`lookup` lands essentially on top of `fixed` (mean gap ₹0.02/payer, 95% CI [-17.80, 16.13] —
indistinguishable) and slightly *ahead* of `greedy` (not significant). Against `mre`, the gap is
real: **lookup - mre = -₹13.50/payer, 95% CI [-28.32, -1.14], significant** — the same magnitude
and significance as `fixed`'s gap to `mre` on this split. Robust across the E_MANUAL sensitivity
sweep ({100, 150, 250} — `lookup`'s numbers don't move at all, since the table doesn't depend on
E_MANUAL; `mre`'s edge over it holds throughout). Full report:
[`reports/dev/BENCHMARK.md`](reports/dev/BENCHMARK.md) (`make bench`'s own default output path,
kept separate from the locked `reports/BENCHMARK.md` above by construction — see
`evaluation/runner.py --out-dir`'s docstring for why that split-namespaced default exists).

Read plainly: a naive table over the two most obvious variables captures almost nothing beyond
`fixed`'s blind schedule — it does not "capture a lot," on this problem, at this population scale.
The real, statistically significant edge is between `mre`/`oracle` and everything simpler
(`fixed`, `greedy`, `lookup` all cluster together), which is a materially stronger answer to "could
deterministic code replace this?" than the plan's own anticipated fallback ("if the GBM beats it by
little, say so") — here it beats it by a lot, on the one axis (`dev`-split timing optimisation) this
benchmark can actually measure. This doesn't rescue Section 8's locked test-split result (`fixed`
still wins there on gross rupees, for the hazard-pricing reasons explained above) — P0b wasn't run
against the sealed split and won't be, by the same discipline. It does answer the red-team question
this exercise was built to pre-empt.

## 9. Metrics

Generated by script, not by hand: [`reports/BENCHMARK.md`](reports/BENCHMARK.md) (benchmark) and
[`reports/calibration.png`](reports/calibration.png) + `reports/calibration_metrics.json`
(success-model calibration — the GBM was already well-calibrated out of the box, ECE ≈1.2%;
isotonic calibration provided no measurable benefit at this corpus size and was kept anyway per
"a credible negative result outranks a fabricated positive one," not tuned until it looked better).
Live operational metrics in Prometheus text format at `GET /metrics`: cases by state, attempts
consumed, policy denials by reason code, decline-cause normalization source counts (dictionary vs.
fuzzy vs. LLM vs. abstained), notice generation source counts, and validator repair count.

Screenshots of all three dashboard screens (case detail, benchmark, audit — captured against a
freshly-seeded demo, zero console errors): [`docs/screenshots/`](docs/screenshots/). Timed
5-minute submission-video script: [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md).

## 10. Setup / testing / limitations / future work

### Setup (three commands, no cloud account)

```bash
make dev              # create venv, install deps
make up                # start Postgres, run migrations
make demo-seed         # seed a curated, repeatable demo scenario (see below)
```

Then in a separate terminal: `.venv/bin/python -m uvicorn app.api.app:app --reload` (run from the
repo root) and visit `http://localhost:8000/dashboard/index.html`.

`make demo-seed` wipes and reseeds three hand-picked cases, one per docs §I.5 workflow —
`CYC-0-RECOVERY` (W1, automatic recovery), `CYC-0-HOPELESS` (W2, stop-and-escalate — see
`scripts/demo_seed.py`'s docstring for an honesty note on how this one is constructed),
`CYC-0-BLOCKED` (W3, the compliance-blocked "demo failure", sorted to the top of the case list) —
plus 40 real `dev`-split payers through the same live ingestion path, so the dashboard doesn't look
suspiciously empty. Run it right before a live demo/recording, not hours in advance: `make test`
also truncates these tables via the integration test fixtures. All three dashboard screens were
verified in an actual headless browser (Chrome via CDP, not just curl) — case-list click-through,
the benchmark screen's dynamic honesty note, and a live kill-switch activate/deactivate round trip
— zero console errors. For the full aggregate-statistics replays instead: `make seed-payers` then
`make replay-fixed` (P0 only) or `make replay-compare` (all three live policies, 300 payers each).

```bash
make check             # lint + strict mypy + full test suite
make bench              # the paired benchmark (dev split; --split test is the one locked run)
make demo-predictability   # standalone proof the timing signal is real, no DB needed
```

### Testing

`make check` runs Ruff, mypy in strict mode, and the full pytest suite (`make test` alone for just
tests) — 212 tests as of this writing, all deterministic, no network calls in CI (the LLM
components are tested with the real call path mocked at the SDK boundary, not skipped).
`tests/integration/test_chaos.py` and `tests/simulator/` cover the failure matrix explicitly:
duplicate/out-of-order webhook delivery, stale events after case closure, mid-plan mandate
revocation, a simulated successfully-injected LLM, rail 5xx and ambiguous-timeout handling, and the
four-attempt cap enforced from three independent code paths.

### Limitations — named before a reviewer finds them

1. **The balance and credit-day model is naive relative to real payer behaviour.** It's a
   documented, stated causal model (linear balance decay from a modelled credit day, a logistic
   funds-sufficiency function) — not fit to any real transaction data, because MRE has no legitimate
   access to a real payer's bank balance or salary date without their explicit, consented
   Account-Aggregator-mediated authorization (see `docs/SIGNAL_LEGITIMACY.md` for the full
   verified/consent-required/not-available breakdown — this was checked, not assumed).
2. **The system optimises each cycle independently**, which is provably suboptimal across a
   mandate's lifetime. The value function accepts a mandate-continuation term
   (`γ · expected_future_value`) as an input but does not compute it — `domain/planner.py` says so
   in its own module docstring. A planner that ignores this term will, in principle, sometimes
   burn a mandate to optimise one cycle.
3. **The revocation/opt-out hazard model is a fixed constant I invented**
   (`optout_hazard_cost=5.0`, `revoke_hazard_cost=20.0` in `PlannerConfig`), not the logistic
   regression over annoyance features the design called for. Since the stopping rule is sensitive
   to it, it is the first thing to validate against real data — and Section 8's locked benchmark
   result is a direct, empirical demonstration of why that validation matters: the planner's
   hazard-avoidance trade-off could not be credited in a benchmark with no stochastic revocation
   model to observe its benefit. Checked directly while building the demo seed script
   (`scripts/demo_seed.py`): with `E_MANUAL=150` and these hazard constants, the stopping rule's
   real economic threshold only fires at essentially *zero* probability — even p=0.001 makes
   continuing worth more than stopping for any realistic mandate amount, since E_manual is a fixed
   cost and the expected value of continuing scales with the amount. `STOP_AND_ESCALATE` is a real,
   tested, first-class DP action (`domain/planner.py`), but under the current default cost
   parameterization it is reachable in practice only near the p≈0 edge case, not across a
   meaningfully wide "this probably won't work" band — another concrete reason these constants need
   real data before they're trustworthy.
4. **The trained success model does not currently discriminate on decline cause.**
   `app/policies/live.py` correctly threads each case's real, normalized cause into the scorer
   (`GET /events` → `/cases/{id}` will show it), but `app/ml/corpus.py`'s synthetic label generator
   draws `cause` independently of the simulated outcome, so the model never had a real
   cause→outcome relationship to learn — `score_slots(cause=MANDATE_REVOKED)` and
   `score_slots(cause=INSUFFICIENT_FUNDS)` currently produce nearly identical probabilities on an
   otherwise-identical payer/slot grid (checked directly: 0.830 vs. 0.824 mean). The
   timing/balance signal is real and does drive genuine differentiation (see
   `scripts/demo_predictability.py`); the cause signal, mechanically wired end to end, is not yet
   load-bearing. Fixing it means changing the training corpus's causal structure — deliberately not
   done days before the deadline, since both `evaluation/runner.py` and `app/policies/live.py`
   retrain from that corpus on every invocation, and changing it now would silently break Section
   8's locked benchmark result's reproducibility guarantee.

### Out of scope, stated plainly

Voice. Real money. Real Razorpay mandate execution APIs (blocked on UPI Autopay test-mode KYC —
see `docs/RAZORPAY_TESTMODE_FINDINGS.md`; the simulator was built to need zero dependency on this
being resolved). Multi-tenant auth. Kubernetes. A vector database. An agent framework. Card-network
specifics. Cross-border. Merchant onboarding.

### Future work

A real, calibrated revocation/opt-out hazard model tied to contact frequency, and a multi-cycle
evaluation metric that can actually credit avoided annoyance — the correct way to test the half of
MRE's value proposition this benchmark's current scope structurally cannot measure (see Section 8).
The mandate-continuation term. An AFA consent flow once Razorpay UPI Autopay test-mode access
unblocks. Per-merchant kill-switch and contact-cap configuration promoted from constants to real
admin-editable state. Making the training corpus's `cause` label causally connected to its
simulated outcome (Limitation 4), then re-running the full locked benchmark once, deliberately,
as a new evaluation — not a silent drift of the existing one.

---

## 11. Where this sits next to Razorpay's own direction

Razorpay's own Agent Studio, launched at FTX'26, ships a production **Subscription Recovery
Agent**: it reads a failed payment's cause, applies retry logic beyond a fixed schedule, and
escalates to a live voice call (ElevenLabs) when retries aren't enough —
[Agent Studio launch](https://razorpay.com/blog/agent-studio-ai-agents-by-razorpay/),
[product page](https://razorpay.com/agent-studio/). MRE was built, and the PRD chosen, before
that launch and independently of it — but it targets the identical problem Razorpay judged worth
shipping a named agent for. That's the strongest evidence available that this project's thesis
wasn't invented for a buildathon: the market case is externally confirmed, not asserted.

The two systems differ in scope, not in philosophy. Razorpay's own description of Agent Studio's
guardrails — "deterministic mathematical guardrails and SHA-256 cryptographic idempotency locks,"
agents that can run in a review-first mode where the agent drafts and a human/deterministic layer
decides before anything executes
([guardrails post](https://razorpay.com/blog/razorpay-agent-studio-principles-guardrails-and-merchant-control/))
— is the same split this codebase makes at a fraction of the scale: the LLM drafts (decline
classification, notice text), a deterministic validator and policy engine decide, and money
movement is never something the AI outputs (Section 4, Section 7). MRE has no voice channel, no
merchant-facing guardrail console, and runs against a simulator rather than Razorpay's real rails
— it is a research-grade proof of the decision layer (an exact, auditable optimizer under RBI's
4-attempt/notice constraints), not a competing product. The honest claim is narrower than
"validated by a billion-dollar company" — it's that the deterministic-guardrail-around-an-LLM
pattern this project converged on independently is the same pattern Razorpay's own platform team
is now publicly shipping.

---

*Full engineering history — every bug found, every architectural decision reconsidered, and why —
is in `CLAUDE.md` (working log), [`docs/ENGINEERING_LOG.md`](docs/ENGINEERING_LOG.md) (the "what
broke" narrative, pulled out on its own), `docs/ADR/` (numbered decision records), and
[`docs/SECURITY_REVIEW.md`](docs/SECURITY_REVIEW.md) (the docs §L.3 red-team exercises, run for
real — one of six found a genuine bug, now fixed).*
