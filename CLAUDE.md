# Working agreement

- Implement ONE phase per session. Stop at that phase's Definition of Done.
- Write tests FIRST for anything in domain/policy, domain/planner, domain/fsm.
- No new dependency without a one-line justification in docs/ADR/.
- Never add: redis, celery, kafka, langchain, langgraph, a vector db,
  an ORM beyond SQLAlchemy Core, or a UI component library beyond Tailwind.
- domain/ stays pure: no network, no DB, no clock. Time is injected.
- Every schema change is a numbered migration. Never edit an applied one.
- After each phase: run `make check`, commit, then STOP and report what
  changed and what is deliberately not yet implemented.

## Phase sequence

| Phase | Component | Definition of done |
|---|---|---|
| 0 | Repo, Docker, CI, shells | `make check` green in CI |
| 1 | Schema, FSM, policy engine | 100% branch coverage on policy + fsm; property tests pass |
| 2 | Generator + simulator | `make seed` works; simulator rejects a 5th attempt; test hash committed |
| 3 | Ingestion, worker, outbox, P0 | 500-mandate fixed-policy replay completes |
| 4 | Success model + calibration | reports/calibration.png committed with ECE before/after |
| 5 | DP planner + stopping rule | DP matches brute-force enumeration on small horizons |
| 6 | Normaliser, notice generator, validator | Zero validator escapes; normaliser F1 reported |
| 7 | Policy wiring, ledger, chaos suite | Chaos tests green in CI |
| 8 | Benchmark, oracle, sensitivity | `make bench` reproducible byte-for-byte |
| 9 | Dashboard, deploy, README | Fresh clone -> demo in three commands |

Current status: Phase 3 done. Real Postgres wired (docker-compose + a
committed, idempotent scripts/migrate.py runner). Idempotent event
ingestion (app/ingest.py) for mandate.cycle.due / debit.succeeded /
debit.failed, exposed thinly over HTTP (app/api/app.py's POST /events) and
covered by real integration tests against live Postgres + a real running
simulator server (tests/integration/, not mocks). Worker + outbox
(app/workflows/worker.py) implementing docs §H.3's reserve-then-dispatch
ordering, with per-step commits. P0 fixed-schedule baseline
(app/policies/fixed.py) — the D+1/D+3/D+7 strawman. `make replay-fixed`
runs the full loop over 500 real generated mandates (dev split only — the
sealed test split stays untouched per docs §J.5/§T) end to end: 500/500
reach a terminal state, ~12s wall clock.

Found and fixed one real correctness bug along the way, now guarded by a
regression test (tests/integration/test_worker_pipeline.py::
test_afa_gated_cycle_reaches_abandoned_not_stuck_forever): a cycle whose
amount exceeds the AFA threshold has every attempt denied at the policy
gate (no AFA consent flow exists yet), so it never consumes a real 4th
attempt and never reached a terminal state on its own —
worker.sweep_exhausted_plans() now resolves these to ABANDONED once a
plan's steps are exhausted.

Deliberately not yet implemented (Phase 3 simplifications, documented in
worker.py): per-merchant contact cap and quiet hours are hardcoded
defaults, not real config; AFA consent flow doesn't exist yet
(afa_satisfied is always False); merchant/global kill switches have no
admin surface; mandate.revoked / notification.opted_out ingestion is
deferred to Day 4.

Phase 4 done. Found and closed a real gap before training anything:
simulator/decline.py's success probability was flat with respect to
timing (only issuer + chronic-failure-propensity), which would have let a
success model learn "issuer predicts outcome" while learning nothing
about *when* to retry — hollowing out Phase 5's planner, whose entire
value proposition is exploiting timing. Added a balance-cycle model
(days-since-credit-day -> expected balance -> logistic funds-sufficiency
probability), backward-compatible (optional kwargs, flat fallback when
absent) so Phase 3's worker/replay path is unaffected — verified
byte-identical replay output before/after.

app/ml/features.py (pure MandateSnapshot -> array, train/serve-skew
defence), app/ml/corpus.py (labeled corpus from train/calibration/dev
splits — test stays sealed), app/ml/train.py (HistGradientBoostingClassifier),
app/ml/calibrate.py (isotonic fit on `calibration`, Brier/ECE evaluated on
held-out `dev`, reliability diagram with per-bin sample counts annotated
so a sparse-bin artifact is never a hidden gotcha), app/ml/registry.py
(content-addressed model versioning). `make train` runs the whole
pipeline in ~2s and writes reports/calibration.png +
reports/calibration_metrics.json (both committed).

Honest finding, reported not hidden: the GBM was already well-calibrated
out of the box (ECE ~1.2%); isotonic calibration provided no benefit and
slightly increased ECE on held-out data at this corpus size. Kept as-is
per docs §I.17 ("a credible negative result outranks a fabricated
positive one") rather than tuned until it looked better.

Phase 5 core done: app/domain/planner.py — exact backward induction over
(slot, attempts-remaining, notice-state), ~28x5x4 states, solves in
microseconds. Verified against an independently-coded, unmemoized
top-down recursive brute-force enumeration of the same decision problem,
at the root state and at every reachable state, plus randomized small
configs via Hypothesis — not just re-deriving the same algorithm a second
way. Monotonicity properties hold (more budget / higher success
probability never lowers value). STOP_AND_ESCALATE is a first-class
action; the stopping rule is literally the DP's own max-comparison at
every state, not a bolted-on threshold.

Found and fixed a real tie-breaking bug while writing the correctness
tests, not a typo: when `e_manual` is time-invariant, IDLE-until-later-
STOP and STOP-now become exactly value-equal, and Python's max() silently
preferred whichever action was listed first — which was IDLE. The *value*
was always correct; the *chosen action* on that tie was an arbitrary
artifact of list order, and it told a worse story ("waited around, then
escalated" instead of "escalated immediately" for identical EV). Fixed by
ordering candidates in explicit tie-break priority (ATTEMPT > STOP >
NOTIFY > IDLE) rather than leaving it to insertion order.

Documented simplifications, consistent with docs §N.7's own cut-order
item #5: notice validity is a single "slots until ready" integer with no
modelled expiry (the real authorize() independently re-enforces the true
7-day cap regardless of what the planner assumed — a mismatch surfaces as
the §W3 demo scenario, not a silent bug); revocation/opt-out hazards are
fixed constants, not the eventual logistic hazard model (Phase 6/7); the
mandate-continuation term (`gamma * expected_future_value`) is an input
the planner accepts but doesn't compute.

Phase 5 fully done: `mre` and `greedy` are now live policies, wired into
real ingestion. New: migrations/0002_payers.sql (+ `make seed-payers`) —
payer attributes now persist in Postgres, not just transiently in
data.generator; app/ml/inference.py bridges the trained Phase 4 artifact
to a real payer's context, scoring calibrated P(success) per slot;
app/policies/mre.py walks the DP's optimal policy table into the same
(step_type, scheduled_for) shape app/policies/fixed.py produces, so the
existing Phase 3 worker/outbox needs zero changes to execute it; a real
"assume failure" simplification, documented in mre.py's module docstring,
lets this reuse Phase 3's pre-commit-schedule infrastructure instead of
needing a dynamically re-planning worker (deferred to Phase 7+).
app/policies/greedy.py is the ablation docs §F.1 calls for — same
calibrated model, naive one-step-lookahead timing, no DP. ingest.py now
routes a DP root-action of STOP_AND_ESCALATE straight to
ESCALATING -> AWAITING_MANUAL with zero attempts consumed (docs §W2),
verified end-to-end against live Postgres.

Found and fixed a second real bug, this one via scripts/replay_compare.py
actually running MRE against 300 real mandates end-to-end — not
hypothetical: every one of MRE's scheduled attempts was being denied
RBI_NOTICE_NOT_SATISFIED, because worker.py hardcoded "a notify always
covers the attempt exactly 1 day later" (true for `fixed`, which always
pairs them adjacently, so this went unnoticed since Phase 3) — false for
MRE, whose DP is free to notify early and IDLE, waiting for a better slot,
before attempting. Fixed properly: migrations/0003 adds
plan_steps.covers_debit_at, set explicitly by whichever policy builds the
schedule (fixed pairs it immediately; mre tracks the pending notify index
through its walk and back-fills it once it knows which attempt actually
uses that notice) rather than assumed by the worker. Confirmed via
scripts/replay_compare.py before/after: MRE went from 225/300 recovered
(75 wrongly abandoned) to 296/300, matching the baselines exactly.

Comparative smoke result (scripts/replay_compare.py, `make
replay-compare` — NOT the rigorous paired Day-5 benchmark; independent
draws per policy, not a shared realised world, documented as such in the
script): all three recover the same count (296/300) on this batch, but
`mre` and `greedy` recover ~24-25% more rupees than `fixed` with
fewer-or-equal attempts. MRE vs greedy specifically are close in this
run — reported honestly as inconclusive at this scale/parameterization
rather than stretched into a bigger claim; resolving that properly (more
samples, bootstrap CIs, a true paired world) is exactly what Phase 8
exists for, not skipped, deferred on purpose.

Phase 6 done — both remaining MUST-HAVEs (M9, M10) built and wired into
the real pipeline, not left standalone. app/ai/client.py: the one place
the Anthropic SDK is touched, model claude-haiku-4-5 (an explicit,
documented choice from docs §K.2, not a cost-driven default — see
docs/ADR/0004). No credentials required for correctness: an absent
ANTHROPIC_API_KEY degrades every caller to its documented fallback (docs
§M.1), verified as the actual failure mode empirically (the SDK raises a
bare TypeError with no key, not a catchable SDK exception — checked
before writing the catch, not assumed).

app/ai/normalizer.py: dictionary -> fuzzy (hand-rolled Levenshtein, no
new dependency) -> LLM -> UNKNOWN, output hard-validated (enum
membership, evidence_span must be a literal substring — the
hallucination check), cached by input hash. docs §L.3's red-team
exercise run for real in tests/ai/test_normalizer.py: all 4 adversarial
suffixes in data/taxonomy.yaml, against a *simulated successfully-
injected* model, confirming the validator — not the model's good
behaviour — bounds the damage to one rejected/UNKNOWN classification.

app/ai/validator.py + app/ai/notice.py: the LLM drafts, a pure
(no-LLM-needed) validator decides — required RBI fields, opt-out
instruction, every number grounded in a caller-built whitelist (scoped
honestly to numbers, not full proper-noun NER — documented in the module
docstring), no threats/fabricated consequences/manufactured urgency,
per-channel length caps. One repair attempt with the validator's own
errors fed back; two rejections or no LLM at all hard-falls to a static
template that's self-consistent with its own validator by construction.

Then wired both into the live path (built-but-unused isn't done):
ingest.py's ingest_debit_failed now normalizes the real seq-1 raw_reason
and persists canonical_cause/cause_confidence/cause_source — columns
that existed in the schema since Phase 1 but were never populated until
now. worker.py's notify-step processing now calls generate_notice
instead of a hardcoded f-string. Both replay_fixed.py and
replay_compare.py re-run byte-identical to before this wiring — zero
behavioural regression, confirms this was purely connecting two
already-correct components to something real.

38 new tests in tests/ai/ (normalizer + validator + notice), all
deterministic, no network in CI. 160 tests green (was 121 at the end of
Phase 5).

Phase 7 (event completeness + chaos suite + webhook signing) done.

mandate.revoked / notification.opted_out ingestion (app/ingest.py):
mandate-scoped, not cycle-scoped — repo.non_terminal_cycles_for_mandate +
ingest._abandon_in_flight_cycles resolve every in-flight cycle on the
mandate through the FSM's existing MANDATE_REVOKED/OPTED_OUT edges
(domain/fsm.py already had them, unused until now) immediately, rather
than relying on the policy gate to slowly deny its way through every
remaining plan_step and eventually get swept under the dishonest
"plan_exhausted" label. AWAITING_MANUAL + MANDATE_REVOKED deliberately has
no FSM edge (a human is already on it) — that asymmetry is real, not an
oversight, and is commented where the code checks for it.

Found and fixed two more real gaps while wiring this in (found by writing
the chaos tests, not by inspection):

1. Out-of-order webhook delivery — a debit outcome arriving before its
   mandate.cycle.due (docs §M.1's chaos matrix: webhooks are at-least-once,
   NOT ordered) — used to either hit a raw AssertionError or leak a
   Postgres ForeignKeyViolation. Now app.ingest.UnknownCycleError, raised
   before any write inside the same transaction as the event insert (so a
   legitimate retry after the real cycle.due lands isn't falsely treated as
   a duplicate), surfaced over HTTP as a clean 409.

2. worker.py's _handle_delivered blindly assumed the cycle was still
   EXECUTING whenever the rail finally answered. An attempt already
   dispatched to the outbox can't be recalled from the rail — so a
   mandate.revoked arriving between dispatch and the rail's answer could
   silently resurrect an already-ABANDONED cycle back to RECOVERED on a
   late "success". Now guarded: the cycle's actual current state is
   checked before applying the EXECUTING-rooted FSM transition; the rail
   outcome is still recorded honestly either way (a distinct audit action,
   attempt_outcome_after_cycle_resolved, makes the mismatch visible rather
   than silent).

Webhook signing (app/api/app.py): HMAC-SHA256 over the raw request body,
hmac.compare_digest, same RAZORPAY_WEBHOOK_SECRET mechanism
scripts/webhook_capture.py's spike tool already proved against real
Razorpay deliveries. When no secret is configured (local dev, CI, replay
scripts) unsigned requests are accepted — a documented degraded mode, not
a silent gap, since refusing to boot without a secret is the wrong failure
mode for a judge's local `make up`.

13 new tests (tests/integration/test_chaos.py, tests/integration/
test_api.py): out-of-order delivery for both debit outcome types, mid-plan
revocation/opt-out cancelling pending steps, idempotency, revocation on an
already-terminal cycle (flag-only no-op), the mid-flight resurrection
race, signature accept/reject in every configuration, the two new event
types over real HTTP. 173 tests green (was 160). Re-ran both
replay_fixed.py and replay_compare.py after every change in this phase —
byte-identical to Phase 6's numbers throughout (492/500, 296/296/296).

Not done, correctly deferred rather than silently dropped: the AFA
consent flow (afa_satisfied is still always False — genuinely blocked on
Razorpay UPI Autopay KYC, documented in
docs/RAZORPAY_TESTMODE_FINDINGS.md, not a Phase 7 scoping choice) and the
merchant/global kill-switch admin surface both need a real UI surface to
be meaningful, so they move to Phase 9 (dashboard) rather than being
half-built here as bare config flags nobody can flip. merchant_name in
notices still falls back to merchant_id (no display-name column yet,
noted in worker.py).

Phase 8 in progress: the rigorous paired benchmark. Before building it,
found and fixed a real gap that would have quietly undermined it: the live
simulator HTTP path (what every replay script and the real worker
actually calls) was NOT the timing-sensitive model Phase 4 built. Two
compounding bugs — simulator/app.py's ExecuteRequest never carried
mean_balance/balance_volatility/credit_day at all (decide_outcome always
took its flat fallback), and worker.py's outbox payload hardcoded
payer_id=None and omitted issuer_code/chronic_fail_propensity entirely for
every worker-dispatched attempt (sequences 2-4 — exactly the ones a
policy actually schedules). Net effect: the trained success model was
correctly built with full timing context (app/ml/corpus.py always used
it), but the mechanism it was predicting had, until now, never actually
varied with what it predicted. Fixed in both places; also reseeded outcome
draws on (payer_id, scheduled_for, sequence_no) instead of idempotency_key
so two different policies attempting for the same real payer at the same
real moment see the identical draw — a shared realised world, required
for Phase 8's paired design to mean anything. 10 new tests. Full details
and the honest before/after replay-fixed numbers (492/500, 1.56M rupees ->
469/500, 467K rupees — expected, not a regression) are in the commit
message.

This mattered for the numbers already reported after Phase 5:
scripts/replay_compare.py's ~24-25% MRE/greedy edge over fixed was
measured *before* this fix and is now known to be partly an artifact of
the bug (flat probabilities meant every policy's retries had the same
±0.78 chance regardless of timing) rather than a clean measurement of
planning skill. Not hidden — corrected.

evaluation/runner.py built: same-realised-world paired batches across
fixed/greedy/mre/oracle, a nonparametric paired bootstrap 95% CI on the
rupee gap between every policy pair, an oracle (P3) that runs the
identical DP solver fed the *true* simulator probability instead of the
model's estimate (a perfect-information ceiling), an E_MANUAL sensitivity
sweep, and — closing a gap scripts/replay_fixed.py's own docstring
already flagged — a realistic per-payer spread of due dates instead of
one shared date (a shared date put every payer's 3 remaining attempts in
the identical 14-day window, structurally capping how much timing skill
could show up regardless of policy). 11 new unit tests on the pure pieces
(bootstrap_ci, paired_diffs, the due-date spread). `make bench` (dev,
n=300) and `make bench-sensitivity` are wired up.

A validation run on `dev` (n=500, not yet the final locked number — see
below) after all of the above:

    policy    recovered  rate    rupees       attempts
    fixed     473        94.6%   493,824.01   670
    greedy    467        93.4%   491,645.96   672
    mre       476        95.2%   500,562.70   682
    oracle    476        95.2%   498,514.74   681

    mre vs greedy: -17.83 rupees/payer, 95% CI [-35.73, -3.48] -- significant
    mre vs fixed:  -13.48 rupees/payer, 95% CI [-32.45,   1.10] -- not quite
    mre vs oracle:  +4.10 rupees/payer, 95% CI [ -5.83,  17.45] -- not significant

Reported honestly, not spun: this is a real but modest edge (~1.4% more
rupees than fixed, statistically significant against greedy specifically),
not the ~25% figure from the pre-fix smoke test. mre landing essentially
on top of oracle (and even fractionally above it in this one realized
sample — sampling noise on a finite batch, not a contradiction, since
oracle is optimal in expectation under the true probabilities, not
guaranteed to win every realized draw) is itself informative: at this
population/horizon/budget, the trained model already captures nearly all
the achievable timing signal — the remaining headroom above fixed is
small because roughly half of any batch's payers have a due date placed
such that their credit_day falls outside the reachable 14-day/3-attempt
window no matter how smart the policy is. This is a legitimate structural
property of the problem (real NPCI attempt cap + real retry horizon), not
a benchmark artifact — matches docs §I.17's standing instruction that a
credible negative/modest result outranks a fabricated positive one.

E_MANUAL sensitivity sweep (dev, n=400, {100, 150, 250}): the ranking held
at every value — mre beat greedy significantly throughout, and mre's edge
over fixed stayed positive and in the same rough magnitude (+15.82, +15.82,
+14.03 rupees/payer) without flipping sign. Robust on dev.

THE LOCKED RUN (docs §J.5/§T — test split touched exactly once, done, not
repeatable): `--split test --n 500 --n-boot 5000 --sensitivity`, run once,
by hand, with the user's explicit go-ahead after the dev sensitivity sweep
came back clean.

    policy    recovered  rate    rupees       attempts
    fixed     481        96.2%   415,700.10   685
    greedy    473        94.6%   410,329.34   676
    mre       474        94.8%   412,213.85   685
    oracle    475        95.0%   411,238.50   682

    fixed vs mre:    +6.97 rupees/payer, 95% CI [ 1.68, 13.86] -- significant
    fixed vs greedy: +10.74 rupees/payer, 95% CI [ 1.28, 22.90] -- significant
    fixed vs oracle:  +8.92 rupees/payer, 95% CI [ 0.39, 20.26] -- significant

This REVERSES the dev-split direction: fixed recovers significantly MORE
gross rupees than mre, greedy, and even oracle on the one locked test
batch. Reported exactly as measured — this is not spun, and it is not
being treated as a bug to explain away.

Investigated rather than accepted at face value (querying the already-
committed DB from this same run, not a re-roll — no new random draws):
first hypothesis was that gross recovered-rupees unfairly ignores the
E_MANUAL cost mre/oracle pay when they escalate (fixed never escalates —
it has no stopping rule, so it always burns attempts regardless of odds).
Checked directly: escalations are rare (mre: 2/500, oracle: 2/500, greedy:
5/500) and E_MANUAL=150 is small next to a ~864-rupee average recovery —
netting out the escalation cost moves mre's total by only ~300 rupees
(412,213.85 -> 411,913.85). Does not explain the gap. Ruled out.

The real explanation: mre/oracle's DP explicitly prices
`optout_hazard_cost`/`revoke_hazard_cost` (domain/planner.py's
PlannerConfig — fixed, documented, made-up constants standing in for "an
extra low-odds attempt/notify risks annoying the payer into opting out or
revoking the mandate entirely") into every ATTEMPT/NOTIFY decision, so on
a handful of genuinely low-probability slots the DP rationally declines to
attempt (a few more cycles quietly run out of plan steps and land in
ABANDONED via sweep_exhausted_plans's honest "plan_exhausted" path, rather
than via a real 4th consumed attempt) where fixed's blind schedule tries
anyway and — since the shared realised world means it's asking about the
same real slot — occasionally succeeds. mre/oracle's abandoned counts
(24, 23) are correspondingly a bit higher than fixed's (19). This is the
DP being rational against a value function that includes a real, named
cost domain/policy.py and PlannerConfig were built to represent — but
neither the simulator nor this benchmark's headline metric can currently
observe or credit that cost's *benefit*: nothing here stochastically
revokes a mandate or records an opt-out as a consequence of contact
frequency (docs §N.7 already flags the revocation hazard model as "fixed
constant, not the eventual logistic hazard model" — an explicit, prior
scope cut, not something this benchmark run discovered new), and even if
it did, a single-cycle metric wouldn't capture a payer who didn't churn on
a *later* cycle because they weren't over-contacted on this one. So the
benchmark, exactly as scoped, structurally cannot demonstrate the half of
MRE's value proposition that depends on avoided annoyance/revocation —
only the timing-optimization half, where (per the dev-split runs above)
its edge over fixed was already small and inconsistent in sign at this
population scale.

Honest bottom line for the final report: MRE recovers statistically
indistinguishable-to-slightly-less gross rupees per cycle than a policy
with no stopping rule at all, on the one locked measurement. Its more
defensible claim, backed by what this benchmark actually measured, is
narrower than "recovers more money" — RBI-compliant by construction
(docs §W3's demo), with a timing-optimization edge that's real on dev but
small and did not survive the one locked test run, plus a
hazard-avoidance rationale that's architecturally real (the DP prices it)
but not something this benchmark's current scope (no stochastic
revocation/opt-out model, single-cycle metric) can measure or credit.
Extending the simulator with a real revocation/opt-out hazard tied to
contact frequency, and a multi-cycle metric, would be the correct way to
actually test that half of the thesis — out of scope for the remaining
time before 5 Sept, and explicitly flagged here rather than left
implicit.

User decided (2026-09-02): move to Phase 9 now, frame the Phase 8 result
honestly rather than build a hazard model first. 3 days left to 5 Sept.

Also closed: reports/BENCHMARK.md + reports/benchmark.json now generated
by evaluation/runner.py's write_report() (docs S.2's "generated by
script, not by hand"), regenerated from the exact same locked test-split
run (deterministic — reproduces the numbers above byte-for-byte, not a
new look at test).

Phase 9 in progress.

Dashboard read/admin API (backend/app/api/dashboard.py, migrations/
0004_kill_switches.sql): GET /cases (+ /cases/{id} with plan_steps'
p_success, attempt_intents with normalized cause, notifications,
decisions, audit trail, and a live-computed fixed-schedule counterfactual
— docs FR-12 / Day 6 priority #1), GET /metrics (Prometheus text format,
docs §I.11), GET /audit (chain-validity — recomputes the
audit_ledger_chain trigger's own SQL hash formula, not reimplemented in
Python), GET/POST /admin/kill-switches (the real kill-switch surface
deferred from Phase 7 — worker.py's _build_snapshot now actually reads it
instead of the hardcoded False it carried since Phase 3). 9 new tests,
including one that toggles the switch over real HTTP then proves the
worker actually denies with GLOBAL_KILL_SWITCH — not just a stored flag.

Dashboard frontend (frontend/*.html): 3 screens, built in the plan's
stated priority order — case detail (list + timeline + counterfactual +
denied actions + attempts + audit, "this screen is the demo, build it
first and best"), benchmark (renders reports/benchmark.json + the
calibration plot, honesty note text depends on which direction the locked
result actually came out), audit (chain-validity indicator + kill switch
control). Vanilla HTML/JS + Tailwind's play CDN — no build step. Mounted
at /dashboard and /reports via FastAPI StaticFiles in api/app.py. Smoke-
tested by hand (uvicorn + curl every page/route) — not yet visually
reviewed in an actual browser or screenshotted.

201 tests green (was 189 at the start of this Phase 9 work).

Found and fixed one more real, time-critical bug while running `make
test` today rather than trusting a stale green run: two integration tests
started failing because real-world time (2026-09-02) caught up to this
project's fixed 2026-09-01 demo/test dates. repo.insert_outbox relied on
the outbox table's column default (`next_attempt_at TIMESTAMPTZ NOT NULL
DEFAULT now()`) — real Postgres wall-clock time at insertion — which was
only ever correct by coincidence for a caller (worker.py, every replay
script, every test) operating on an injected/simulated clock. Once real
time passed the fixed 2026-09 dates, a row's DB-default next_attempt_at
could land *after* the simulated `now` the caller was about to query
with — permanently stuck, no error. This would have started silently
breaking `make replay-fixed`/`make bench` from today onward with nothing
in the scripts themselves to catch it. Fixed: insert_outbox now takes
`next_attempt_at` explicitly from the caller instead of the DB default.
Confirmed replay-fixed is still byte-identical (469/500, 467,276.74) and
all 201 tests are green again.

Not done yet: README per docs §S.1's ten-question structure (still says
"Phase 1 in progress"), Mermaid architecture diagram, deploy (docs: "one
platform with managed Postgres, hard cap 3 hours — a flawless documented
docker compose up is worth more than a fragile cloud deploy"), seed/reset
script for a repeatable demo, docs/ENGINEERING_LOG.md, the Day 7 red-team
exercises from docs §L.3, and actually rehearsing the demo end-to-end in
a browser.

Next: continue Phase 9 — README first (it's the cheapest, highest-leverage
remaining item and forces writing the honest Limitations section this
session already has all the material for), then seed/reset script + a
real browser check of the dashboard, then the Mermaid diagram, then decide
on deploy given the explicit "docker compose up is worth more than a
fragile cloud deploy" guidance.
