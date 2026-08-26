"""The simulator's own schema — independent of the main Postgres schema.

SQLite, not Postgres: this service exists to independently enforce the
NPCI attempt cap and execution-window rules even when the caller (our own
worker) is buggy (docs §H.2 — "if the simulator lived inside the app it
would share the app's bugs"). That job needs durable UNIQUE/CHECK
constraints, not SKIP LOCKED-style concurrent-queue semantics, so SQLite
enforces the invariant just as strictly as Postgres would here.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no BETWEEN 1 AND 4),
    idempotency_key TEXT NOT NULL UNIQUE,
    mandate_id TEXT NOT NULL,
    payer_id TEXT,
    amount REAL NOT NULL,
    scheduled_for TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'unknown')),
    raw_reason TEXT,
    executed_at TEXT NOT NULL,
    UNIQUE (cycle_id, sequence_no)
);
"""


@dataclass(frozen=True)
class AttemptRecord:
    cycle_id: str
    sequence_no: int
    idempotency_key: str
    mandate_id: str
    payer_id: str | None
    amount: float
    scheduled_for: str
    outcome: str
    raw_reason: str | None
    executed_at: str


class SequenceCapViolation(Exception):
    """Raised when the DB's own UNIQUE(cycle_id, sequence_no) rejects an insert."""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def reset(self) -> None:
        self._conn.executescript("DROP TABLE IF EXISTS attempts;" + SCHEMA)
        self._conn.commit()

    def get_by_idempotency_key(self, idempotency_key: str) -> AttemptRecord | None:
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return self._to_record(row) if row is not None else None

    def count_attempts(self, cycle_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()
        n: int = row["n"]
        return n

    def insert_attempt(self, record: AttemptRecord) -> None:
        try:
            self._conn.execute(
                """
                INSERT INTO attempts
                    (cycle_id, sequence_no, idempotency_key, mandate_id, payer_id,
                     amount, scheduled_for, outcome, raw_reason, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.cycle_id,
                    record.sequence_no,
                    record.idempotency_key,
                    record.mandate_id,
                    record.payer_id,
                    record.amount,
                    record.scheduled_for,
                    record.outcome,
                    record.raw_reason,
                    record.executed_at,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise SequenceCapViolation(str(exc)) from exc

    @staticmethod
    def _to_record(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            cycle_id=row["cycle_id"],
            sequence_no=row["sequence_no"],
            idempotency_key=row["idempotency_key"],
            mandate_id=row["mandate_id"],
            payer_id=row["payer_id"],
            amount=row["amount"],
            scheduled_for=row["scheduled_for"],
            outcome=row["outcome"],
            raw_reason=row["raw_reason"],
            executed_at=row["executed_at"],
        )
