"""Apply migrations/*.sql, in filename order, to DATABASE_URL.

Idempotent: applied filenames are tracked in `schema_migrations`, so
re-running is a no-op for anything already applied. Per CLAUDE.md, an
applied migration is never edited — only new numbered files are added.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def main() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
        applied = {
            row[0] for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                print(f"skip (already applied): {path.name}")
                continue
            print(f"applying: {path.name}")
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )


if __name__ == "__main__":
    main()
