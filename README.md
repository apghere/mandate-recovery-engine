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

## 9. Metrics

Generated by script, not by hand: [`reports/BENCHMARK.md`](reports/BENCHMARK.md) (benchmark) and
[`reports/calibration.png`](reports/calibration.png) + `reports/calibration_metrics.json`
(success-model calibration — the GBM was already well-calibrated out of the box, ECE ≈1.2%;
isotonic calibration provided no measurable benefit at this corpus size and was kept anyway per
"a credible negative result outranks a fabricated positive one," not tuned until it looked better).
Live operational metrics in Prometheus text format at `GET /metrics`: cases by state, attempts
consumed, policy denials by reason code, decline-cause normalization source counts (dictionary vs.
fuzzy vs. LLM vs. abstained), notice generation source counts, and validator repair count.

## 10. Setup / testing / limitations / future work

### Setup (three commands, no cloud account)

```bash
make dev              # create venv, install deps
make up                # start Postgres, run migrations
.venv/bin/python -m uvicorn app.api.app:app --reload   # run from the repo root
```

Then visit `http://localhost:8000/dashboard/index.html`. To see real data: `make seed-payers`
then `make replay-fixed` (or `make replay-compare` for all three live policies), which populate
the database the dashboard reads from.

```bash
make check             # lint + strict mypy + full test suite
make bench              # the paired benchmark (dev split; --split test is the one locked run)
make demo-predictability   # standalone proof the timing signal is real, no DB needed
```

### Testing

`make check` runs Ruff, mypy in strict mode, and the full pytest suite (`make test` alone for just
tests) — 201 tests as of this writing, all deterministic, no network calls in CI (the LLM
components are tested with the real call path mocked at the SDK boundary, not skipped).
`tests/integration/test_chaos.py` and `tests/simulator/` cover the failure matrix explicitly:
duplicate/out-of-order webhook delivery, mid-plan mandate revocation, a simulated successfully-
injected LLM, rail 5xx and ambiguous-timeout handling, and the four-attempt cap enforced from three
independent code paths.

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
   model to observe its benefit.

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
admin-editable state.

---

*Full engineering history — every bug found, every architectural decision reconsidered, and why —
is in `CLAUDE.md` (working log) and `docs/ADR/` (numbered decision records).*
