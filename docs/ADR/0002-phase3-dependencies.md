# ADR 0002 — Phase 3 dependencies: psycopg, python-dotenv; docker-compose scope

- **psycopg[binary]** (runtime): the Postgres driver. Raw parameterized SQL,
  not an ORM — docs §H.2 wants the four-attempt cap to be a database
  constraint the application merely triggers, not something an ORM's query
  builder mediates. CLAUDE.md permits "SQLAlchemy Core" as the ORM ceiling;
  we're staying below even that, since Core wouldn't buy us anything for
  the small number of hand-written queries this needs, and psycopg3 alone
  is a lighter footprint.
- **python-dotenv** (runtime): loads `.env` once at process start
  (`backend/app/config.py`), so `DATABASE_URL` etc. don't need manual
  `export`/`source` before every command. Tiny, ubiquitous, no ORM/queue/
  agent-framework overlap with the never-add list.

**docker-compose.yml scope, Phase 3:** Postgres only. `api`/`worker`/
`simulator` run directly via `.venv/bin/uvicorn` during development — they
don't need containers to develop against a real Postgres, and writing three
Dockerfiles now, before those services' shapes are finalized, would be
premature. Full multi-service compose (docs §H.1's api/worker/db/simulator/
web) is deferred to Day 6 ("Full bring-up in three commands" is a demo-day
deploy requirement, §N Day 6 — not a Day-2 correctness one).
