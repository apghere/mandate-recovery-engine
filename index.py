"""Vercel Python entrypoint (docs/DEPLOY.md).

Lives at the repo root, not under api/, on purpose: Vercel's zero-config
Python entrypoint search looks for index.py (among others) at the root or
inside src//app/ — NOT inside api/, which is the older file-based-
function convention. Mixing that convention with framework-preset
auto-detection (Vercel detected FastAPI from pyproject.toml) caused every
route to 404 in this project's first deploy attempt: the framework
preset was routing every request to this function, but the request path
Vercel handed the ASGI app didn't line up with what api/index.py plus a
catch-all rewrite produced. Moving here and dropping the manual
catch-all rewrite (vercel.json now only rewrites /dashboard/* and
/reports/* to static files) fixed it — verified against the live
deployment, not assumed.

Vercel's Python builder detects the `app` ASGI object exported from this
file and wraps it directly — no uvicorn, no Mangum adapter needed. This
is a thin path-setup shim, not a second copy of the application: the real
FastAPI app is backend/app/api/app.py, the exact same object `uvicorn
app.api.app:app --app-dir backend` serves locally. One app, two entry
points, on purpose — a second implementation here would drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
for _path in (_REPO_ROOT / "backend", _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.api.app import app  # noqa: E402,F401 — Vercel imports this module-level name
