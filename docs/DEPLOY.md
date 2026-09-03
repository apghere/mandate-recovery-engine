# Deploying to Vercel

Read this before running anything — it says plainly what this deployment
is and isn't, then gives exact commands.

## Scope, stated honestly

This deployment is a **publicly-viewable, read-first browsing surface**
over a once-seeded demo dataset — the same `CYC-0-RECOVERY` /
`CYC-0-HOPELESS` / `CYC-0-BLOCKED` + 40 background cases
`scripts/demo_seed.py` produces locally, seeded once against a real
Postgres, then served from there. It is **not** a live transactional
environment: there is no worker process running anywhere (there isn't
one locally either — see below), no separately deployed simulator, and no
cron job dispatching attempts in real time. `POST /events` will accept
and persist an event exactly as it does locally (idempotent ingestion,
real DB writes, real audit trail), but nothing will then execute the
resulting plan against a rail — because nothing does that continuously
even in local dev; `make demo-seed` / `replay_fixed.py` / `replay_compare.py`
all drive the worker functions by hand against a simulated clock,
advancing it to completion, rather than a real background loop existing
anywhere in this codebase today. Deploying that honestly rather than
pretending Vercel added a capability the project never built.

The documented **local** `docker compose up` + recorded demo remains the
primary submission artifact per the PRD's own guidance ("a flawless
documented docker compose up is worth more than a fragile cloud deploy").
This Vercel deployment is an additional, polished, always-on link a judge
can click without running anything themselves — a browsing convenience,
not a replacement.

## What's already done (this session)

- `api/index.py` — thin entrypoint exporting the same FastAPI `app` object
  `uvicorn app.api.app:app` serves locally. Verified importable.
- `vercel.json` — routes `/dashboard/*` and `/reports/*` straight to the
  static `frontend/`/`reports/` directories (fast, no bundling needed);
  everything else to the Python function. Pins the Python runtime to 3.12
  (matches `pyproject.toml`'s `mypy` target — if your Vercel account
  doesn't yet offer 3.12, change `"runtime"` to `"python3.11"`, the code
  has no 3.12-only syntax).
- `requirements.txt` — pinned to the exact versions in `.venv` right now
  (tested), not a re-derivation from `pyproject.toml`.
- `.vercelignore` — excludes `.venv/`, `tests/`, caches, screenshots.
- `app/config.py` already reads `DATABASE_URL` from the environment — no
  code change needed to point it at a hosted Postgres instead of the
  local docker-compose one.
- A user-local Node.js + Vercel CLI, installed to `~/.local/node` (no
  sudo, doesn't touch system Python/Node) and added to `PATH` via
  `~/.bashrc` — `vercel --version` should work in any new terminal.

## What only you can do (needs your account)

### 0. Confirm the CLI is on your PATH

Open a **new** terminal (so it picks up the `~/.bashrc` change) and run:

```bash
vercel --version   # should print something like "Vercel CLI 59.11.2"
```

If that prints `command not found`, run `source ~/.bashrc` first.

### 1. Get a Postgres reachable from the internet

Easiest path — Vercel's own Postgres storage (Neon-backed), same account,
no second signup:

```bash
vercel login                      # opens a browser, your account
cd /home/apghere/mandate-recovery-engine
vercel link                       # creates/links the Vercel project
```

Then in the Vercel dashboard for this project: **Storage → Create →
Postgres** → connect it to the project. Vercel injects `DATABASE_URL`
(and a pooled variant) into the project's environment automatically —
copy that connection string down, you need it in step 2.

(If you'd rather use Neon directly — neon.tech, free tier, no card —
create a project there instead and paste its connection string wherever
`$DATABASE_URL` appears below. Either way, the string must include
`?sslmode=require`, which both providers include by default.)

### 2. Migrate and seed that database from your machine

Run these locally, pointed at the *hosted* database, once:

```bash
cd /home/apghere/mandate-recovery-engine
export DATABASE_URL="<paste the connection string from step 1>"
.venv/bin/python scripts/migrate.py
PYTHONPATH=backend .venv/bin/python -m scripts.demo_seed
```

Confirm it worked before moving on:

```bash
.venv/bin/python -c "
import psycopg, os
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    print(c.execute('select count(*) from recovery_cycles').fetchone())
"
```

Should print a count around 43 (3 curated + 40 background), matching the
local run from earlier this session.

### 3. Set the same env vars on Vercel, then deploy

```bash
vercel env add DATABASE_URL production      # paste the same connection string
vercel env add ANTHROPIC_API_KEY production # optional -- absent key degrades
                                             # gracefully to documented fallbacks
                                             # (README §4), doesn't break the app
vercel env add RAZORPAY_WEBHOOK_SECRET production  # optional, same as local

vercel --prod
```

That last command prints the live URL. Open `<url>/dashboard/index.html`
and click through exactly as you did locally — case list, `CYC-0-HOPELESS`
detail, `<url>/dashboard/benchmark.html`, `<url>/dashboard/audit.html`.

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
| Function duration | **10s hard cap, not configurable higher** | Cold path: ~1.2s module import + ~2s model retrain + a DB round trip; warm path: low tens of ms | OK, real margin — see the matplotlib fix below for why this wasn't always true |
| Function invocations | 1M/month | A judge clicking through 3 screens a few times | Trivial |
| Active CPU | 4 hours/month | Model retrain is CPU-bound (~2s) but only pays that cost once per cold container, not per request | Comfortable unless something hammers the endpoint continuously, which nothing here does |
| Provisioned memory | 360 GB-hrs/month | One small function, low request volume | Comfortable |
| Function bundle size | 250MB uncompressed | scikit-learn + numpy + fastapi + psycopg + anthropic, **matplotlib removed** (below) | Should fit with margin, not independently verified end-to-end (no account here) |
| Bandwidth | 100GB/month | A few KB of HTML/JSON per click, one small PNG | Trivial |
| Cron jobs | Not used by this deploy at all | — | N/A — irrelevant either way |
| Neon storage | 0.5GB/project | 43 seeded rows across a handful of tables — low single-digit MB | Trivial |
| Neon compute | 100 CU-hours/month, autosuspends after 5min idle at $0 | Bursts of activity around clicks, otherwise suspended | Comfortable |

**One real fix made because of this analysis, not just noted:** the live
request-serving path (`api/index.py` → `app/api/app.py` → `app/policies/
live.py` → `app/ml/calibrate.py`) used to import `matplotlib` at module
scope purely because `calibrate.py` also contained the (offline-only)
reliability-diagram plotting code. Split into
`app/ml/calibration_plot.py` (matplotlib, only imported by
`scripts/train.py` and tests) and a matplotlib-free `app/ml/calibrate.py`
(just `fit_isotonic`, the one thing the live path needs). Verified, not
assumed: `'matplotlib' in sys.modules` is `False` after importing
`api/index.py` fresh; full test suite (212 tests), ruff, and mypy strict
all still pass. `requirements.txt` no longer lists matplotlib — smaller
function bundle, faster cold start, nothing lost, since
`reports/calibration.png` is a static file the deployed function never
regenerates (`vercel.json` routes `/reports/*` around the function
entirely).

`vercel.json` also now pins `"maxDuration": 10` explicitly on the
function — Hobby's ceiling either way, but stated rather than left
implicit.

## Remaining risks, named rather than discovered live

- **Bundle size still isn't independently verified end-to-end** — no
  Vercel account was available in this session to actually run
  `vercel --prod` and see the real number. Should fit comfortably given
  the matplotlib removal, but if the first deploy does fail on size, say
  so and the next thing to trim is `anthropic` (only needed if you set
  `ANTHROPIC_API_KEY` — could be made a lazy/optional import if it ever
  comes to that).
- **`POST /events` works, nothing then acts on it** — see Scope above.
  Don't demo live event submission on the deployed link; demo it locally
  where the seed/replay scripts actually drive resolution.
