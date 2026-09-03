"""Vercel Python entrypoint (docs/DEPLOY.md).

Vercel's Python builder detects the `app` ASGI object exported from this
file and wraps it directly -- no uvicorn, no Mangum adapter needed. This
is a thin path-setup shim, not a second copy of the application: the real
FastAPI app is backend/app/api/app.py, the exact same object `uvicorn
app.api.app:app --app-dir backend` serves locally. One app, two entry
points, on purpose -- a second implementation here would drift.

/dashboard/* and /reports/* are NOT served through this function on
Vercel -- vercel.json rewrites those straight to the static frontend/ and
reports/ directories (faster, and keeps StaticFiles' local-filesystem
mounts, which app.py already guards with `.is_dir()`, as a no-op here
rather than something this function needs to bundle).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (_REPO_ROOT / "backend", _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.api.app import app  # noqa: E402,F401 -- Vercel imports this module-level name
