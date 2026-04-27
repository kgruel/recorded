"""Storage layer: schema, canonical SQL, timestamp + id helpers, connection management."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

# Status values. Keep as plain strings — no enum overhead.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  key             TEXT,
  status          TEXT NOT NULL,
  submitted_at    TEXT NOT NULL,
  started_at      TEXT,
  completed_at    TEXT,
  request_json    TEXT,
  response_json   TEXT,
  data_json       TEXT,
  error_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_kind     ON jobs(status, kind);
CREATE INDEX IF NOT EXISTS idx_jobs_kind_submitted  ON jobs(kind, submitted_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_key_active
  ON jobs(kind, key)
  WHERE key IS NOT NULL AND status IN ('pending', 'running', 'completed');
"""

# Canonical column list — used to keep INSERT/SELECT in sync.
COLUMNS = (
    "id",
    "kind",
    "key",
    "status",
    "submitted_at",
    "started_at",
    "completed_at",
    "request_json",
    "response_json",
    "data_json",
    "error_json",
)

SELECT_BY_ID = f"SELECT {', '.join(COLUMNS)} FROM jobs WHERE id = ?"

# Used by recorded.last(); kind=None matches any.
SELECT_LAST = f"""
SELECT {', '.join(COLUMNS)} FROM jobs
WHERE (? IS NULL OR kind GLOB ?)
ORDER BY submitted_at DESC
LIMIT ?
"""

INSERT_PENDING = """
INSERT INTO jobs (id, kind, key, status, submitted_at, request_json)
VALUES (?, ?, ?, 'pending', ?, ?)
"""

UPDATE_RUNNING = """
UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='pending'
"""

UPDATE_COMPLETED = """
UPDATE jobs
   SET status='completed', completed_at=?, response_json=?, data_json=?
 WHERE id=? AND status='running'
"""

UPDATE_FAILED = """
UPDATE jobs
   SET status='failed', completed_at=?, error_json=?
 WHERE id=? AND status IN ('pending', 'running')
"""


def now_iso() -> str:
    """Lex-sortable ISO8601 UTC, microsecond precision, `Z` suffix.

    Used for every timestamp we write.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def new_id() -> str:
    """`uuid4().hex` — unique, no monotonic guarantees."""
    return uuid.uuid4().hex


def open_connection(path: str) -> sqlite3.Connection:
    """Open a connection in WAL mode with sane defaults.

    `check_same_thread=False` because the worker (phase 2) and any
    `asyncio.to_thread` call use the connection from helper threads.
    Concurrency is serialized at the SQLite level (WAL).
    """
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
