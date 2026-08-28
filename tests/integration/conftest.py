"""Fixtures for integration tests: a real Postgres connection (docker
compose's `db` service must be up — `make up`) and a real simulator server
running over HTTP in a background thread, exercised exactly as the worker
exercises it in production (docs §H.2 — no in-process shortcuts that would
let a bug in one side hide from the other).
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import psycopg
import pytest
import uvicorn
from app.config import get_settings
from psycopg.rows import DictRow, dict_row

from simulator.app import create_app

_TABLES_TO_RESET = (
    "audit_ledger",
    "decisions",
    "notifications",
    "outbox",
    "attempt_intents",
    "plan_steps",
    "plans",
    "cycles",
    "mandates",
    "events",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="session")
def simulator_base_url() -> Iterator[str]:
    port = _free_port()
    app = create_app(db_path=":memory:")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "simulator test server failed to start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def db(simulator_base_url: str) -> Iterator[psycopg.Connection[DictRow]]:
    settings = get_settings()
    try:
        conn = psycopg.connect(settings.database_url, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc}) — run `make up` first")
        return
    conn.execute(f"TRUNCATE {', '.join(_TABLES_TO_RESET)} RESTART IDENTITY CASCADE")
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()
