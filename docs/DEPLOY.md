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

## Known risks, named rather than discovered live

- **Cold starts are slow-ish.** `app/policies/live.py` retrains the GBM +
  isotonic calibration from the synthetic corpus on first use per warm
  container (`make train` takes ~2s locally) — a request that lands on a
  cold container after idle time will pay that ~2s once, then subsequent
  requests on the same warm container reuse the cached artifact. Not a
  bug, just worth knowing before a judge sees a slow first click.
- **Bundle size.** `scikit-learn` + `numpy` + `matplotlib` (imported
  because `app/policies/live.py` imports `app/ml/calibrate.py`, which
  imports `matplotlib` at module scope for its plotting function even
  though the live path never calls it) add up. Should fit Vercel's
  Hobby-tier function size limit, but this was never actually deployed
  and tested end-to-end in this session (no Vercel account available
  here) — if the first `vercel --prod` fails on function size, the fix is
  splitting `fit_isotonic` out of `calibrate.py` into a matplotlib-free
  module; say so and it can be done in a few minutes.
- **`POST /events` works, nothing then acts on it** — see Scope above.
  Don't demo live event submission on the deployed link; demo it locally
  where the seed/replay scripts actually drive resolution.
