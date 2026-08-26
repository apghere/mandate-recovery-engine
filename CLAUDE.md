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

Current status: Phase 2 done (payer-population generator, taxonomy, sealed
test-split hash, and the independent simulator service with DB-enforced
4-attempt cap + chaos hooks all in place and tested). Phase 3
(ingestion/worker/outbox/P0 baseline, wiring the real Postgres schema from
migrations/0001_core.sql) is next.
