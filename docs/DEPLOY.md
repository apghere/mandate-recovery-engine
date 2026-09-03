# Deploying to Vercel

**Status: deployed and verified live** at
[mandate-recovery-engine.vercel.app](https://mandate-recovery-engine.vercel.app)
(2026-09-03). All three dashboard screens and the `/cases`, `/audit`,
`/reports/*` endpoints checked directly against the live URL — real
seeded data, zero console errors, matches the local UI pixel-for-pixel.
This doc is now the record of how it was actually deployed and the one
real bug fixed to get there, not a speculative runbook.

## Scope, stated honestly

This deployment is a **publicly-viewable, read-first browsing surface**
over a once-seeded demo dataset — the same `CYC-0-RECOVERY` /
`CYC-0-HOPELESS` / `CYC-0-BLOCKED` + 40 background cases
`scripts/demo_seed.py` produces locally, seeded once against a real
Postgres (Neon, via Vercel's Storage integration), then served from
there. It is **not** a live transactional environment: there is no
worker process running anywhere (there isn't one locally either — see
below), no separately deployed simulator, and no cron job dispatching
attempts in real time. `POST /events` will accept and persist an event
exactly as it does locally (idempotent ingestion, real DB writes, real
audit trail), but nothing will then execute the resulting plan against a
rail — because nothing does that continuously even in local dev;
`make demo-seed` / `replay_fixed.py` / `replay_compare.py` all drive the
worker functions by hand against a simulated clock, advancing it to
completion, rather than a real background loop existing anywhere in this
codebase today.

The documented **local** `docker compose up` + recorded demo remains the
primary submission artifact per the PRD's own guidance ("a flawless
documented docker compose up is worth more than a fragile cloud deploy").
This Vercel deployment is an additional, polished, always-on link a judge
can click without running anything themselves — a browsing convenience,
not a replacement.

## Architecture as actually deployed

- **`index.py`** (repo root) — the Vercel Python entrypoint. Re-exports
  the exact same FastAPI `app` object `uvicorn app.api.app:app --app-dir
  backend` serves locally — one implementation, not a fork.
- **`vercel.json`** — just `functions.index.py` (`maxDuration: 10`,
  `excludeFiles` trimming `tests/`, `docs/`, `migrations/`,
  `evaluation/`, `scripts/`, etc. out of the function bundle — none of
  those are touched by the live-serving import chain, verified by grep
  before excluding, not assumed). **No `rewrites` at all** — see "The one
  real bug" below for why.
- **No separate static build.** `/dashboard/*` and `/reports/*` are
  served by the *same* FastAPI app's own `StaticFiles` mounts
  (`backend/app/api/app.py`), exactly like local dev — `frontend/` and
  `reports/` are deliberately **not** excluded from the function bundle
  (they're tiny; excluding them broke the app's own `.is_dir()` guard,
  see below).
- **Neon Postgres**, provisioned via Vercel's Storage → Marketplace →
  Neon integration, auto-injecting `DATABASE_URL` and several pooled/
  non-pooling variants into the Production environment.
- **`requirements.txt`** (repo root) — pinned runtime deps, deliberately
  missing `matplotlib` (see the free-tier section below).

## The one real bug, and the fix

The first two deploy attempts returned `404 {"detail":"Not Found"}` from
*every* path, including ones that should always exist on a bare FastAPI
app (`/openapi.json`, `/docs`). Root cause, found by testing rather than
guessing: Vercel auto-detected this project as a **FastAPI framework
preset** (from `fastapi` in `pyproject.toml`), and a recent platform
change — "Internal rewrites in backend framework projects now route
requests using the rewritten destination path" — means a configured
`rewrite`'s *destination* string becomes the actual path handed to the
ASGI app, not just a hint about which function to invoke. The original
`vercel.json` had `{"source": "/(.*)", "destination": "/api/index"}` as
a catch-all, so *every* request arrived at the FastAPI app with path
`/api/index` — never `/cases`, `/audit`, `/openapi.json`, whatever was
actually requested. Everything 404'd because nothing in the app is
routed at `/api/index`.

Separately, the original entrypoint lived at `api/index.py` — a
convention for the *older* file-based Python functions model, not the
zero-config entrypoint search Vercel's current docs describe (`app.py`,
`index.py`, etc. at the repo root, or inside `src/`/`app/` — not `api/`).
Mixing that with framework-preset auto-detection was the second half of
the confusion.

Fixed by moving to the documented zero-config shape: entrypoint at
**`index.py`** (repo root), and **removing the rewrites entirely** —
letting the framework preset route every request straight to the same
FastAPI app, path preserved, exactly as the Vercel docs describe ("the
app you run locally deploys as-is"). `/dashboard/*` and `/reports/*`
work through the app's own `StaticFiles` mounts, not a rewrite — which
also meant undoing the earlier (well-intentioned but wrong) decision to
`excludeFiles: frontend/**` from the function bundle, since that starved
the app's own mount of the files it needed to serve. Verified against
the live deployment after each change, not assumed fixed from reasoning
alone.

## How it was actually set up (for redeploys / reference)

1. `vercel login`, `vercel link` — done interactively (needs a browser).
2. Vercel dashboard → project → **Storage → Create Database → Neon
   (Serverless Postgres)** → connect to Production. This auto-injects
   `DATABASE_URL`, `POSTGRES_URL`, `POSTGRES_URL_NON_POOLING`, and
   several `PG*` vars.
3. `vercel env pull <file> --environment=production` to get the real
   connection strings locally, then, using `POSTGRES_URL_NON_POOLING`
   (not the pooled one — DDL migrations and demo_seed.py's many
   sequential statements are more reliable outside a transaction-mode
   pooler; a first seed attempt through the pooled string stalled
   partway through, leaving inconsistent state that had to be redone):
   ```bash
   export DATABASE_URL="<POSTGRES_URL_NON_POOLING value>"
   .venv/bin/python scripts/migrate.py
   PYTHONPATH=backend .venv/bin/python -m scripts.demo_seed
   ```
   Seeding over the real network to Neon takes a few minutes, not the
   few seconds it takes against local Docker Postgres — run it with a
   long timeout or in the background, not the default 2-minute one.
4. `vercel --prod` — no env vars needed for `DATABASE_URL` specifically
   since step 2 already injected it into Production.
5. Verify: `curl` the API routes directly, and load
   `<url>/dashboard/index.html` in a real browser (headless Chrome via
   CDP, in this session) and check for console errors — don't trust a
   green build log alone.

`ANTHROPIC_API_KEY` / `RAZORPAY_WEBHOOK_SECRET` were left unset —
documented fallbacks (README §4) handle their absence gracefully.

## Free-tier fit — analysed against the actual documented limits, not guessed

Checked against current published numbers (2026-09-03), not assumed. Both
Vercel Hobby and Neon Free are $0, no card required to sign up (Neon
never asks; Vercel Hobby's own card-on-file, if the flow shows one, is
for identity/spend-alerting, not billing — Hobby cannot incur charges,
it throttles or blocks instead). Hobby's terms restrict it to personal,
non-commercial use — a buildathon/portfolio submission with no paying
customers and no revenue reads as squarely inside that, not the
SaaS/e-commerce/client-work case the restriction targets.

| Limit | Hobby/Free ceiling | This project's actual usage | Fit |
|---|---|---|---|
| Function duration | **10s hard cap, not configurable higher** | Cold path: ~1.2s module import + ~2s model retrain + a DB round trip; warm path: low tens of ms | OK, real margin |
| Function invocations | 1M/month | A judge clicking through 3 screens a few times | Trivial |
| Active CPU | 4 hours/month | Model retrain is CPU-bound (~2s) but only pays that cost once per cold container, not per request | Comfortable |
| Provisioned memory | 360 GB-hrs/month | One small function, low request volume | Comfortable |
| Function bundle size | 250MB standard / larger on Fluid compute | Reported 310.79MB uncompressed at build time — Vercel auto-optimized rather than failing (Fluid compute's larger allowance) | **Deployed and working**, confirmed live |
| Bandwidth | 100GB/month | A few KB of HTML/JSON per click, one small PNG | Trivial |
| Cron jobs | Not used by this deploy at all | — | N/A |
| Neon storage | 0.5GB/project | 43 seeded rows across a handful of tables — low single-digit MB | Trivial |
| Neon compute | 100 CU-hours/month, autosuspends after 5min idle at $0 | Bursts of activity around clicks, otherwise suspended | Comfortable |

**The matplotlib fix, made because of this analysis:** the live
request-serving path (`index.py` → `app/api/app.py` → `app/policies/
live.py` → `app/ml/calibrate.py`) used to import `matplotlib` at module
scope purely because `calibrate.py` also contained the (offline-only)
reliability-diagram plotting code. Split into `app/ml/calibration_plot.py`
(matplotlib, only imported by `scripts/train.py` and tests) and a
matplotlib-free `app/ml/calibrate.py` (just `fit_isotonic`, the one thing
the live path needs). Verified: `'matplotlib' in sys.modules` is `False`
after importing the entrypoint fresh; 212 tests, ruff, mypy strict all
still pass. `requirements.txt` no longer lists matplotlib —
`reports/calibration.png` is a static file the deployed function never
regenerates, confirmed loading correctly on the live site regardless.
