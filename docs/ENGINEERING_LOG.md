# Engineering Log — what broke and how I recovered

This is the honest version of the build, written for the "what broke"
question specifically. Every entry below is a real bug or gap found while
building or running the system — not a hypothetical — with the symptom,
the root cause, the fix, and how the fix was verified. Nothing here was
found by inspection alone; each was caught by running something real
(a test, a replay script, a browser, a clock) and watching it misbehave.
Entries are in the order they happened. CLAUDE.md carries the same
material folded into each phase's status; this file pulls it out as its
own narrative because "what broke" is a graded question on its own.

Solo build, Claude Code as engineering partner, working agreement in
`CLAUDE.md`: one phase per session, tests first for `domain/`, honest
reporting of negative results over spun ones (docs §I.17: "a credible
negative result outranks a fabricated positive one").

---

## Phase 3 — ingestion, worker, outbox, P0

**Bug: AFA-gated cycles never reached a terminal state.**
A cycle whose amount exceeds the AFA (Additional Factor of
Authentication) threshold has every attempt denied at the policy gate,
because no AFA consent flow exists yet. Denied attempts don't consume a
plan step, so the cycle just sat there — never recovered, never
abandoned, forever `EXECUTING` in name only. Found running the full
500-mandate replay, not by reading the code: a chunk of cycles simply
never showed up in the terminal-state counts.

Fix: `worker.sweep_exhausted_plans()` now resolves a cycle to `ABANDONED`
once its plan's steps are exhausted, regardless of why they were
exhausted. Regression test:
`test_afa_gated_cycle_reaches_abandoned_not_stuck_forever`.

---

## Phase 4 — success model + calibration

**Gap (not a bug, but load-bearing): the simulator's success probability
didn't depend on timing.**
`simulator/decline.py`'s outcome draw only used issuer code and a
chronic-failure propensity — flat with respect to *when* a retry
happened. Training a model against that would have taught it "issuer
predicts outcome" and nothing about timing, which hollows out the entire
point of a DP planner (Phase 5) whose value proposition is exploiting
timing. Caught before any training ran, while reviewing what the model
would actually be learning from.

Fix: added a balance-cycle model to the simulator — days-since-credit-day
→ expected balance → logistic funds-sufficiency probability — with
optional kwargs and a flat fallback when absent, so Phase 3's already-
working replay path was untouched. Verified byte-identical replay output
before/after adding it.

**Honest negative result: isotonic calibration didn't help.**
The trained GBM was already well-calibrated out of the box (ECE ≈1.2%).
Isotonic calibration on top of it slightly *increased* held-out ECE at
this corpus size. Kept the uncalibrated model rather than tuning until
the calibration step looked useful — reported as a negative result, not
hidden or worked around, per docs §I.17.

---

## Phase 5 — DP planner, then wiring it into live policies

**Bug: a real tie-break bug, not a typo, in the DP's `max()`.**
When `e_manual` is time-invariant, "idle now and stop later" and "stop
now" become exactly value-equal at some states, and Python's `max()`
silently prefers whichever action was listed first — which happened to
be `IDLE`. The chosen *value* was always correct; the chosen *action* on
a tie was an arbitrary artifact of list order, and it told a worse story
("waited around, then escalated" instead of "escalated immediately" for
identical expected value). Found while writing the correctness tests
(brute-force equivalence + Hypothesis), not by reading the code.

Fix: explicit tie-break priority (`ATTEMPT > STOP > NOTIFY > IDLE`)
instead of relying on insertion order.

**Bug: every MRE-scheduled attempt was being denied.**
Wiring `mre`/`greedy` into `scripts/replay_compare.py` and running it
against 300 real mandates end-to-end — not a unit test — showed every
single MRE attempt failing the policy gate with
`RBI_NOTICE_NOT_SATISFIED`. Root cause: `worker.py` hardcoded the
assumption "a notify always covers the attempt exactly 1 day later,"
true for `fixed` (which always pairs them adjacently) but false for MRE,
whose DP is free to notify early and idle, waiting for a better slot,
before attempting. This had been silently true since Phase 3 because
`fixed` was the only policy that had ever run.

Fix: `migrations/0003` adds `plan_steps.covers_debit_at`, set explicitly
by whichever policy builds the schedule, instead of assumed by the
worker. Before/after via `replay_compare.py`: MRE went from 225/300
recovered (75 wrongly abandoned) to 296/300, matching the baselines.

---

## Phase 7 — event completeness + chaos suite

**Bug: out-of-order webhook delivery crashed or leaked a DB error.**
A debit outcome arriving before its `mandate.cycle.due` (webhooks are
at-least-once, not ordered — docs §M.1's chaos matrix) used to either hit
a raw `AssertionError` or leak a Postgres `ForeignKeyViolation` straight
through. Found writing the chaos tests specifically to probe this, which
is the point of a chaos suite — it's supposed to find exactly this.

Fix: `app.ingest.UnknownCycleError`, raised before any write inside the
same transaction as the event insert (so a legitimate retry after the
real `cycle.due` lands isn't falsely treated as a duplicate), surfaced
over HTTP as a clean 409.

**Bug: a late rail response could resurrect an abandoned cycle.**
`worker.py`'s `_handle_delivered` blindly assumed the cycle was still
`EXECUTING` whenever the rail finally answered. An attempt already
dispatched to the outbox can't be recalled — so a `mandate.revoked`
arriving between dispatch and the rail's answer could silently flip an
already-`ABANDONED` cycle back to `RECOVERED` on a late "success."

Fix: the cycle's actual current state is checked before applying the
`EXECUTING`-rooted FSM transition; the rail outcome is still recorded
honestly either way, via a distinct audit action
(`attempt_outcome_after_cycle_resolved`) that makes the mismatch visible
instead of silent.

---

## Phase 8 — the paired benchmark

**Gap found before building the benchmark, not after: the live simulator
path had never actually varied with timing.**
Two compounding bugs, found by reviewing what the benchmark would
actually be measuring before trusting it: `simulator/app.py`'s
`ExecuteRequest` never carried `mean_balance`/`balance_volatility`/
`credit_day` at all, so `decide_outcome` always took its flat fallback;
and `worker.py`'s outbox payload hardcoded `payer_id=None` and omitted
issuer/chronic-propensity context entirely for every worker-dispatched
attempt — exactly the ones (sequence 2–4) a real policy schedules. Net
effect: the trained success model was built correctly with full timing
context, but the mechanism it was predicting had never actually varied
with what it predicted, in production or in any replay script.

Fix: wired the missing fields through `simulator/app.py`,
`simulator_client.py`, `worker.py`, and both replay scripts. Also
reseeded outcome draws on `(payer_id, scheduled_for, sequence_no)`
instead of `idempotency_key`, so two different policies attempting the
same real payer at the same real moment see the identical draw — the
shared-realised-world property Phase 8's paired design depends on for
its comparisons to mean anything.

**This retroactively corrected an earlier reported number.** Phase 5's
`replay_compare.py` smoke result — MRE/greedy recovering ~24–25% more
rupees than fixed — was measured before this fix and is now known to be
partly an artifact of the flat-probability bug (every policy's retries
had the same ±0.78 chance regardless of timing), not a clean measurement
of planning skill. Not hidden after the fact — flagged in CLAUDE.md the
same day the fix landed, and again here.

**The locked test-split result reversed direction from dev, and I did
not spin it.**
The `dev`-split validation run showed MRE beating fixed by a small but
positive margin. The one locked, single-use `test`-split run (n=500,
n_boot=5000, run once with explicit go-ahead) showed the opposite:
`fixed` recovered significantly *more* gross rupees than `mre`, `greedy`,
and even `oracle`.

    fixed vs mre:    +6.97 rupees/payer, 95% CI [ 1.68, 13.86] -- significant
    fixed vs greedy: +10.74 rupees/payer, 95% CI [ 1.28, 22.90] -- significant
    fixed vs oracle:  +8.92 rupees/payer, 95% CI [ 0.39, 20.26] -- significant

I investigated this rather than accepting or explaining it away.
Escalation cost was the first hypothesis (fixed never escalates, mre/
oracle sometimes do) — checked directly against the already-committed
run, no re-roll: escalations were rare (2–5 per 500) and netted only
~₹300, which doesn't explain a gap in the thousands. Ruled out. The real
explanation: the DP explicitly prices `optout_hazard_cost`/
`revoke_hazard_cost` into every attempt/notify decision — a real,
named cost representing "an extra low-odds contact risks annoying the
payer into opting out or revoking the mandate" — so on some low-
probability slots it rationally declines to attempt, where fixed's blind
schedule tries anyway and (since it's the same real slot in the shared
world) occasionally succeeds. Neither the simulator nor this benchmark's
single-cycle metric can currently observe or credit the *benefit* of
that avoided-annoyance cost, because nothing here stochastically revokes
a mandate as a consequence of contact frequency. So the benchmark, as
scoped, structurally cannot measure the half of MRE's thesis that
depends on avoided revocation — only the timing-optimization half, whose
edge over fixed was already small and inconsistent in sign at this
population size even on `dev`.

This is reported in full in `CLAUDE.md`, `README.md`'s Evaluation
section, and `reports/BENCHMARK.md` (generated by `evaluation/runner.py`,
not written by hand) — not softened, because the plan I'm building this
against says explicitly that a result like this is "a finding to report
honestly, not a bug to hide."

---

## Phase 9 — dashboard, live wiring, demo

**Bug: the outbox's `next_attempt_at` depended on real wall-clock time,
and real time caught up with the project's fixed demo dates.**
Found because `make test` actually started failing on 2026-09-02, not
because anyone went looking. `repo.insert_outbox` relied on the
`next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()` column default —
real Postgres time at insertion. That was only ever correct by
coincidence for a caller (worker, every replay script, every test)
operating on an injected/simulated clock fixed around 2026-09-01. Once
real wall-clock time passed that date, a row's DB-default
`next_attempt_at` could land *after* the simulated `now` the caller was
about to query with — permanently stuck, no error, no log line. This
would have started silently breaking `make replay-fixed`/`make bench`
from that day onward with nothing in the scripts themselves able to
catch it.

Fix: `insert_outbox` now takes `next_attempt_at` explicitly from the
caller instead of the DB default. Confirmed `replay-fixed` is still
byte-identical to before the fix (469/500, ₹467,276.74) and all 201
tests green again. This is the kind of bug a fixed-date demo project is
specifically exposed to, and it's exactly the kind that doesn't show up
until the calendar does the finding for you.

**Architectural gap: the live `/events` endpoint never actually used
MRE.** Since Phase 5, `compute_plan`'s call site in `api/app.py`'s
`_dispatch` always defaulted to the fixed baseline. MRE and greedy were
only ever reachable through replay/benchmark scripts — never through the
product's own API. This means every prior claim about "the live system
runs MRE" was, until this fix, not actually true of the running product,
only of the offline scripts. Found while planning a demo scenario for
docs §W2, not by inspection: writing down what the demo would click
through made it obvious the API path had never been exercised end to
end with MRE live.

Fix: `compute_plan`'s signature widened to `Callable[[date, Cause],
PlanChoice]`; new `app/policies/live.py` builds the real, cause-aware,
payer-aware plan for the live path (lazy cached model artifact, graceful
fallback to fixed when no payer row exists); `_dispatch` now calls it.
2 new tests prove this over real HTTP, not by importing the function
directly.

**Finding, not fixed: the trained model doesn't discriminate on cause.**
Verifying the fix above, I checked `score_slots(cause=MANDATE_REVOKED)`
against `score_slots(cause=INSUFFICIENT_FUNDS)` on an identical payer/
slot grid. The outputs were nearly identical (0.830 vs 0.824 mean).
Root cause: `app/ml/corpus.py`'s synthetic label generator draws `cause`
independently of the simulated outcome, so the GBM never had a real
cause→outcome relationship available to learn in the first place. The
live wiring is structurally correct — the real cause now reaches the
scorer — but it isn't yet behaviourally load-bearing.

Deliberately not fixed before the deadline: changing the training
corpus's causal structure would change what `evaluation/runner.py`
retrains and scores on every invocation, which would silently invalidate
the already-locked, already-reported sealed-test-split benchmark's
reproducibility guarantee, days before submission. Documented as a named
limitation in `README.md` instead of quietly patched around.

**Finding, not a bug: the stopping rule only fires at essentially p=0.**
Building the W2 (stop-and-escalate) demo case, I tried to construct a
"plausible-looking" low-probability payer (large amount relative to
balance, high volatility) to trigger `STOP_AND_ESCALATE` naturally.
Swept a real range of amount/balance/volatility combinations — none of
them triggered it. Because `E_MANUAL` is a fixed cost while continuing's
expected value scales with amount, even p=0.001 beats stopping for any
realistic mandate amount under the current `PlannerConfig` constants.
Only literal p=0.0 crosses the threshold.

Not a bug in the DP — the DP is doing exactly the right comparison for
the constants it was given — but it means the stopping rule is, in
practice, far less reachable than the demo narrative implies. Fixed the
demo script to supply `p_success=[0.0]*28` directly to the DP (the same
technique the existing unit test already used) rather than presenting a
"realistic-looking" case as something the live scorer produced unaided,
and documented the finding as strengthening README's existing
hazard-constants limitation.

**UX bug found by looking at the rendered page, not by curling the
API.** The three curated demo cases (`CYC-DEMO-*`) sorted alphabetically
*behind* all 40 background cases (`CYC-BG-*`), since `repo.list_cycles`
orders by `due_date DESC, id` and every seeded case shares a due date. A
presenter would have had to scroll past 40 rows to find the curated
ones. A curl-only check of `/cases` would never have surfaced this — it
only became obvious looking at the actual rendered table.

Fix: renamed the curated cases to `CYC-0-*` (digit `0` sorts before
uppercase letters), verified with a second screenshot showing all three
at the top.

---

## Day 7 — red-team exercises (docs §L.3)

**Bug: a stale event delivered after case closure crashed with an
unhandled DB exception.** Running the L.3 exercise "deliver a stale event
after case closure and confirm quarantine" for real — closing a cycle via
`debit.succeeded`, then delivering a late `debit.failed` for the same
`cycle_id` under a fresh `external_id` — produced a raw
`psycopg.errors.UniqueViolation` propagating straight through the HTTP
layer as an unhandled 500. `ingest_debit_succeeded`/`ingest_debit_failed`
had never checked whether a cycle was already terminal before reserving
sequence 1, because every *intended* call pattern only ever delivers a
seq-1 outcome once, on a freshly-`DUE` cycle. The mandate-lifecycle
ingestion functions (`mandate.revoked`/`notification.opted_out`) already
had this guard, via `repo.non_terminal_cycles_for_mandate` filtering
before touching anything; the two debit-outcome functions didn't.

Fix: both now check the cycle's current state against `TERMINAL_STATES`
right after fetching it. A terminal cycle gets the event recorded (the
audit trail should show it arrived) and a `stale_event_quarantined` audit
entry, but nothing downstream — no attempt reservation, no FSM
transition, no plan mutation. Verified: the exercise re-run cleanly (200,
state untouched, quarantine entry present, no exception); 2 new
regression tests in `tests/integration/test_chaos.py`; full suite (205,
up from 203), lint, and mypy all green; `make replay-fixed` still
byte-identical (469/500, ₹467,276.74). Full exercise log with the other
five (which found nothing) is in `docs/SECURITY_REVIEW.md`.

## Day 7 — the P0b baseline (docs §T red-team item 2)

**Near-miss: a dev-split benchmark run almost silently overwrote the
locked test-split report.** Building P0b (a deterministic lookup-table
baseline, added to pre-empt "deterministic code could replace the ML
model" per docs §T item 2), the first run of `evaluation.runner` with the
new 5-policy `POLICIES` tuple used the default `--out-dir` (`reports/`).
That directory already held the committed, one-time-locked test-split
`BENCHMARK.md`/`benchmark.json` — this dev-split run overwrote both with
different numbers before I'd noticed. `git status` caught it immediately,
before anything was committed (both files showed modified); restored via
`git checkout -- reports/BENCHMARK.md reports/benchmark.json`, then
re-ran with an explicit `--out-dir reports/dev_p0b_baseline` so the
locked artifact and the new dev-only comparison live in separate files.

Named plainly rather than glossed over: the sealed test split's actual
protection in this moment was a human/agent checking `git status` before
committing, not anything enforced by the tool itself — and this wasn't a
one-off setup mistake either: `make bench`/`make bench-sensitivity` (the
documented, repeatable dev-benchmark commands) both invoke
`evaluation.runner` with no `--out-dir`, so *every* routine `make bench`
run after the locked test result was committed would have silently
clobbered it the same way. Fixed properly, not just documented:
`--out-dir` now defaults to `reports/` only for `--split test` and to
`reports/dev/` for `--split dev` — a routine dev run can no longer share
a path with the locked artifact by construction, regardless of which
command invokes it. Re-ran the dev+sensitivity benchmark against the new
default path to confirm: numbers byte-identical to the first run,
`reports/BENCHMARK.md`/`benchmark.json` untouched (`git status` clean on
both).

## Known bugs / gaps still open at the time of writing

- `feature_hash` is hardcoded to `"n/a"` in `plans` table inserts —
  content-addressed model versioning exists (`app/ml/registry.py`) but
  isn't threaded into every plan-insert call site yet.
- `merchant_name` in generated notices falls back to `merchant_id` — no
  display-name column exists yet.
- AFA consent flow doesn't exist (`afa_satisfied` is always `False`) —
  genuinely blocked on Razorpay UPI Autopay KYC access, not a scoping
  choice; see `docs/RAZORPAY_TESTMODE_FINDINGS.md`.
- The trained model's cause-blindness and the stopping rule's near-p=0
  threshold, both above, are open by deliberate choice given the
  deadline, not by oversight.

## Remaining risk going into submission

The two demo-affecting findings above (cause-blindness, stopping-rule
threshold) mean the W2 demo case is built from a directly-supplied
`p_success=0.0`, not from a naturally-occurring live scenario. This is
disclosed in the script's own docstring and in this log — the risk is a
sharp reviewer asking "would this happen on its own?" and the honest
answer being "not yet, and here's exactly why." That's a defensible
answer because it's true and because the underlying mechanism (the DP's
own stopping comparison) is real and independently tested; it would not
be defensible if it weren't disclosed.
