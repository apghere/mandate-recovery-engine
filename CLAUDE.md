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

README written per docs §S.1's ten-question structure (real Mermaid
diagram, the honest Phase 8 evaluation section, three named limitations)
— committed, setup instructions verified by actually starting the server
with the exact documented command rather than trusting what should work
(caught a real `--app-dir` import-path bug doing this).

Found and fixed a second real architectural gap while planning a demo
scenario for docs §W2: the live `/events` HTTP endpoint has, since Phase
5, ALWAYS used the P0 fixed baseline — MRE/greedy were only ever
reachable through replay/benchmark scripts, never through the product's
own API. `compute_plan`'s signature widened to accept the cause
ingest_debit_failed just normalized; new app/policies/live.py builds the
real, cause-aware, payer-aware compute_plan for the live path (lazy
cached artifact, graceful fixed-fallback with no payer row); api/app.py's
`_dispatch` now uses it. 2 new tests prove it over real HTTP. 203 tests
green.

While verifying this, found and honestly flagged (not fixed, not hidden):
the trained success model does NOT currently discriminate on cause at
all — score_slots(cause=MANDATE_REVOKED) vs.
score_slots(cause=INSUFFICIENT_FUNDS) on an identical payer/slot grid
produce nearly identical probabilities (0.830 vs 0.824 mean). Root cause:
app/ml/corpus.py's synthetic label generator draws `cause` independently
of the simulated outcome, so the GBM never had a real cause->outcome
relationship to learn. The live wiring above is structurally correct (the
real cause now reaches the scorer) but not yet behaviourally load-bearing
for automatic cause-driven stopping. Deliberately not fixed now: doing so
means changing the training corpus's causal structure, which
evaluation/runner.py and app/policies/live.py both retrain from at every
invocation — changing it days before the deadline would silently
invalidate the already-locked, already-committed Phase 8 sealed-test-
split benchmark's reproducibility guarantee. This is now a fourth named
limitation, alongside the other three in README.md.

Demo-scenario implication: the W2 (stop-and-escalate) demo case should
be built around a genuinely low-probability *timing/balance* situation
(amount large relative to mean_balance every reachable day — this
mechanism is real and verified, docs/SIGNAL_LEGITIMACY.md /
scripts/demo_predictability.py), not a MANDATE_REVOKED cause — the latter
would not actually produce a low score via the live path today, and a
demo built on it would be quietly showing something that doesn't work
yet.

scripts/demo_seed.py built and verified. The planned timing/balance-driven
W2 scenario didn't survive contact with reality either: swept a real
range of (amount, mean_balance, volatility) combinations and none of them
made the DP choose STOP_AND_ESCALATE with E_MANUAL=150 -- only literal
p=0.0 does, since E_manual is a fixed cost and continuing's expected value
scales with amount (even p=0.001 beats stopping for any realistic
mandate). So W2's curated case supplies p_success=0.0 directly to the
real DP solver (same technique test_mre_ingestion.py already uses) and
says so in the script's own docstring, rather than presenting it as
something the live scorer produced unaided. Seeds CYC-0-RECOVERY (W1),
CYC-0-HOPELESS (W2), CYC-0-BLOCKED (W3) + 40 real dev-split payers through
the live ingestion path. `make demo-seed` wired up.

Found and fixed while actually looking at the rendered dashboard, not
just curling the API: the three curated cases sorted alphabetically
behind all 40 background cases (CYC-BG-* < CYC-DEMO-*) -- a presenter
would've had to scroll past 40 rows. Renamed to CYC-0-* so they sort
first.

Browser-verified all 3 screens for real (headless Chrome via CDP --
websocket-client + requests, since chromium-cli isn't available in this
environment): clicked into a case from the list, toggled the kill switch
via its actual button (not a raw POST) and watched the display update
live, confirmed the benchmark screen's honesty note correctly reflects
the "fixed won" locked direction. Zero console errors on all three pages.

Remaining Phase 9 items, in priority order given ~3 days left: Mermaid
diagram export as a standalone file (currently only embedded in
README.md, which likely satisfies docs P.1's requirement already --
low priority), the deploy decision (docs' own guidance: "a flawless
documented docker compose up is worth more than a fragile cloud deploy" --
leaning toward not deploying anywhere and relying on the documented local
setup + a recorded demo), docs/ENGINEERING_LOG.md (the "what broke and
how I recovered" graded field -- CLAUDE.md itself is most of the raw
material for this already), and the Day 7 red-team exercises from docs
§L.3 (security review pass).

---

Continuing on the above list, re-grounded against the source PDF (§P.1's
file tree, §L.3's exact six exercises, §S.1/§S.2) via `pdftotext` before
starting, not from memory:

`docs/diagrams/architecture.mmd` exported (docs P.1) — the same Mermaid
source embedded in README.md's Architecture section, copied out as a
standalone file with a short docs/diagrams/README.md explaining the
relationship. No mermaid-cli/Node toolchain exists in this environment;
judged not worth installing solely to produce a static PNG this close to
the deadline (GitHub renders the fenced block natively either way) --
documented as a deliberate call, not silently skipped.

`docs/ENGINEERING_LOG.md` written -- the "what broke and how I
recovered" narrative pulled out of CLAUDE.md's phase-by-phase notes into
its own dedicated, chronological file, since it's a specifically graded
field (docs S.2).

Day 7 red-team exercises (docs §L.3) run for real, not reasoned about:
all six against live Postgres + the real FastAPI app (TestClient, same
technique test_api.py uses) + the real simulator where needed. Five
confirmed the existing design holds (prompt injection bounded by the
validator -- reused test_normalizer.py's existing 4-suffix red-team
test; webhook replayed 50x -> exactly one event/cycle row; forged/missing
HMAC signature both rejected 401, correct one accepted; attempt-cap
override has literally no lever to pull -- the only admin write surface
is the kill switch, which can only block, never grant, and the 5th
attempt is independently rejected by the app's Postgres CHECK/UNIQUE
constraints AND the simulator's own field bound; fabricated notice amount
rejected by the whitelist check). One found a real bug:

**Bug: a stale event delivered after case closure crashed with an
unhandled DB exception**, not a clean quarantine. Running exercise 5 for
real (close a cycle via debit.succeeded, then deliver a late debit.failed
for the same cycle_id under a fresh external_id -- the realistic
at-least-once-redelivery case) produced a raw
`psycopg.errors.UniqueViolation` propagating straight through the HTTP
layer as an unhandled 500. `ingest_debit_succeeded`/`ingest_debit_failed`
had never checked whether a cycle was already terminal before reserving
sequence 1 -- every *intended* call pattern only ever delivers a seq-1
outcome once, on a freshly-DUE cycle, so this was invisible until
red-teamed. The mandate-lifecycle ingestion functions
(mandate.revoked/notification.opted_out) already had this guard via
repo.non_terminal_cycles_for_mandate filtering before touching anything;
the two debit-outcome functions didn't.

Fixed: both now check the cycle's state against `TERMINAL_STATES` right
after fetching it. A terminal cycle gets the event recorded (audit trail
integrity -- it really did arrive) plus a `stale_event_quarantined` audit
entry, but nothing downstream (no attempt reservation, no FSM
transition, no plan mutation). Verified: exercise re-run cleanly (200,
state untouched, quarantine entry present, no exception); 2 new
regression tests in tests/integration/test_chaos.py; full suite (205,
up from 203), lint, and mypy all green; `make replay-fixed` still
byte-identical (469/500, Rs 467,276.74). Full exercise log for all six,
findings and non-findings alike, is in docs/SECURITY_REVIEW.md; the bug
and fix are also folded into docs/ENGINEERING_LOG.md's own Day 7 entry.

README.md updated: links to both new docs files, test count corrected
205 (was stale at 201).

The deploy decision: not deploying to a separate cloud platform. Decided,
not left open -- the source PDF's own N.7 scope-cut order lists "cloud
deployment -> local-only with a recorded demo" as cut #2 (before UI
polish, even), and Day 6's instructions read "a flawless documented
docker compose up is worth more than a fragile cloud deploy; the video is
the artifact that matters." The documented `make dev && make up && make
demo-seed` + one uvicorn command path already reaches a working demo in
three commands (verified for real, including in an actual browser, this
session) -- a fragile Day-6/7 cloud deploy would trade a proven path for
an unproven one under real time pressure, for a requirement the source
material itself explicitly says is worth less than the alternative.

Remaining, in priority order for the final ~2-3 days: full rehearsal of
the demo three times against a wall clock (docs Day 6's own instruction,
not yet done for real -- browser verification happened, a timed rehearsal
hasn't); the P0b deterministic-lookup-table baseline docs §T's red-team
item 2 explicitly recommends shipping ("Deterministic code could replace
the ML model" is called "the strongest technical hit," and pre-empting it
by shipping it as a fifth baseline is called "the strongest possible
response") -- not yet built, flagged to the user as a real scope decision
given remaining time rather than assumed; the 5-minute submission video;
screenshots; final README pass; the v1.0-submission tag.

---

User confirmed (2026-09-02): build P0b. Built and run for real, not left
as a paper baseline.

`backend/app/ml/lookup_baseline.py`: `fit_lookup_table` groups the exact
same `train`-split labeled corpus `app/ml/train.py` fits the GBM on
(`app/ml/corpus.py::generate_corpus("train")` -- same split discipline,
same input data, so the comparison isolates the model, not a difference
in what data each baseline saw) by `(cause, day_of_month)` and averages
the observed success label per bucket, with a documented 3-level backoff
(bucket -> cause -> global) for sparse combinations. `score_slots_lookup`
matches `app/ml/inference.py::score_slots`'s exact output shape (a
`tuple[float, ...]` over slots), so it plugs directly into the existing
`compute_greedy_schedule` (`app/policies/greedy.py`) with zero changes to
scheduling logic -- the only thing P0b swaps out relative to `greedy` is
the source of P(success), which is exactly what §T item 2 asks about.
7 new pure-function tests in tests/ml/test_lookup_baseline.py, including
one against the real train corpus (not just synthetic fixtures).

Wired into evaluation/runner.py as a 5th policy (`lookup`,
`POLICIES = ("fixed", "greedy", "lookup", "mre", "oracle")`) --
`_lookup_compute_plan` mirrors `_greedy_compute_plan` exactly, swapping
`score_slots` for `score_slots_lookup`. `_train_lookup_table` threaded
alongside `_train_artifact` through `run_paired_batch`/
`run_sensitivity_sweep`/`main()`. 212 tests green (was 205), lint + mypy
clean, `make replay-fixed` still byte-identical (469/500, Rs 467,276.74).

**Near-miss caught before it did damage**: the first `make bench`-
equivalent run after wiring this in used the default `--out-dir`
(`reports/`), which silently overwrote the already-committed, LOCKED
test-split `reports/BENCHMARK.md`/`benchmark.json` with a dev-split,
5-policy report -- `git status` caught it immediately (both files showed
modified) before anything was committed. Restored via `git checkout --
reports/BENCHMARK.md reports/benchmark.json`. Worth naming plainly: the
sealed test split's actual protection here was `git status` and a
human/agent checking it before committing, not anything in the tool
itself -- and this wasn't a one-off setup mistake: `make bench`/`make
bench-sensitivity` both invoke evaluation.runner with no `--out-dir`
either, so *every* routine `make bench` run after the locked result was
committed would have clobbered it the same way. Fixed properly, not just
documented: `--out-dir` now defaults to `reports/` only for `--split
test` and to `reports/dev/` for `--split dev`, so a routine dev run can't
share a path with the locked artifact by construction regardless of
which command invokes it. Re-ran dev+sensitivity against the new default
path to confirm byte-identical numbers and a clean `git status` on the
locked files. Full writeup, including this near-miss, is in
docs/ENGINEERING_LOG.md's own Day 7 P0b entry.

**Result, dev split n=500** (docs §T item 2's own anticipated framing:
"if the GBM beats it by little, say so and note that the planner is where
the value lives" -- it beats it by a lot here, which is a stronger
answer, not the fallback one):

    policy    recovered  rate    rupees       attempts
    fixed     473        94.6%   493,824.01   670
    greedy    467        93.4%   491,645.96   672
    lookup    469        93.8%   493,814.67   672
    mre       476        95.2%   500,562.70   682
    oracle    476        95.2%   498,514.74   681

    lookup vs fixed:  +0.02 rupees/payer,  95% CI [-17.80, 16.13] -- not significant
    lookup vs greedy: -4.34 rupees/payer,  95% CI [-21.52, 11.55] -- not significant
    lookup vs mre:   -13.50 rupees/payer,  95% CI [-28.32, -1.14] -- significant
    lookup vs oracle: -9.40 rupees/payer,  95% CI [-25.71,  7.65] -- not significant

`lookup` is statistically indistinguishable from `fixed` and from
`greedy` -- a plain (cause, day-of-month) table captures almost nothing
beyond the blind fixed schedule on this problem, at this population
scale. The real, significant edge sits between {mre, oracle} and
everything simpler ({fixed, greedy, lookup} all cluster together).
Confirmed robust across the E_MANUAL sensitivity sweep ({100, 150, 250}
-- `lookup`'s own numbers don't move, since the table has no E_MANUAL
dependency; `mre`'s edge over it holds at every value). Full report:
reports/dev/BENCHMARK.md (`--out-dir`'s new split-namespaced default —
see the near-miss note above).

This does not change Section 8's locked test-split headline (`fixed`
still wins there on gross rupees, for the hazard-pricing reasons already
investigated) -- P0b was deliberately never run against the sealed split,
consistent with "touched exactly once." What it does do is answer docs
§T item 2 directly and with a stronger result than the plan itself
anticipated needing: the model+planner combination beats a naive
deterministic table by a real, significant margin on the one axis (dev
timing optimisation) this benchmark can measure, even though the locked
test-split result on gross rupees went the other way for `mre` vs
`fixed` overall. Both facts are true and both are reported, not just the
flattering one.

README.md updated with the full P0b writeup in the Evaluation section,
right after the existing honesty paragraph.

---

Final-stretch review (2026-09-03), re-grounded against the repo as source
of truth rather than memory: confirmed the working baseline directly
before touching anything (docs §14 discipline) — `make check` equivalent
run by hand: 171 passed / 41 skipped, ruff clean, mypy clean on its
configured scope (`backend/app`, `data`, `simulator`, `scripts`,
`evaluation` — tests/ is deliberately excluded from strict mode, not a
gap; running mypy against `tests/` directly does surface ~40 untyped-test
errors, which is expected and out of scope, not a regression). Repo audit
found nothing to delete or rename: no stray/junk files, `__pycache__`
correctly gitignored, `.env` correctly untracked, docs/reports structure
matches what CLAUDE.md and README already claim. Commit history (37
commits) already reads as natural, specific, non-robotic — no rewrite
needed.

Did the one piece of research not yet done: checked Razorpay's actual
2026 public direction rather than assuming the original Day-1 research
was still current. Finding, real and load-bearing: Razorpay's own Agent
Studio (launched at FTX'26) ships a production Subscription Recovery
Agent solving this exact problem, and its own guardrails post describes
the identical "LLM drafts, deterministic layer decides" split this
codebase converged on independently. Written up honestly (narrower claim
than "validated by Razorpay," scoped to "same architectural pattern, same
problem class, much smaller and research-grade") as README.md's new
Section 11 — the highest-value, lowest-effort change available with ~2
days left, since a judge who knows Agent Studio will otherwise wonder
whether this was known and left unaddressed.

No other structural changes made. Frontend, benchmark, and Section 8's
honest "fixed won on the locked test split" finding are left exactly as
they are — already correct, already investigated, already reported
straight; re-litigating them now would be spending the remaining time on
restating conclusions rather than closing anything open.
