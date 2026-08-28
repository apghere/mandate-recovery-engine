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
deferred to Day 4. Phase 4 (success model + calibration) is next.
