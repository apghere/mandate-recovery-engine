"""Thin Postgres connection helper. All I/O lives here, in repo.py, and in
adapters/ — domain/ stays pure per CLAUDE.md."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row

from app.config import get_settings


@contextmanager
def get_connection() -> Iterator[psycopg.Connection[DictRow]]:
    settings = get_settings()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn


Conn = psycopg.Connection[Any]
