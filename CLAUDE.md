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

Not yet done, and meaningfully separate follow-on work: wiring the
planner as a live `mre` policy alongside P0 `fixed` (plus a `greedy`
ablation) — bridging real DB case state -> MandateSnapshot -> the trained
Phase 4 model's calibrated probabilities -> planner input, registering it
in the worker/ingestion path the way docs §O.4's Phase 5 prompt describes.
That's the natural next chunk, not a quick addendum to this one.
