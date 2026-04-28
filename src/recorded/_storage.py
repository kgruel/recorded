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

# Reserved kind prefix for library-internal rows (currently: leader heartbeat
# rows under `_recorded.leader`). User-decorated kinds can't start with this
# prefix (rejected at decoration time); the read API
# (`Recorder.query`/`last`) excludes them by default unless the caller asks
# for them explicitly via `kind="_recorded.*"`.
RESERVED_KIND_PREFIX = "_recorded."
RESERVED_KIND_GLOB = "_recorded.*"

# Leader-heartbeat row kind. The leader process (`python -m recorded run`)
# inserts one row of this kind on startup (key=`host:pid`) and updates its
# `started_at` periodically as a liveness signal. `Recorder.is_leader_running`
# checks for any fresh row of this kind; `.submit()` gates on that.
#
# `started_at` doubles as the staleness clock — see WHY.md::Lifecycle (the
# `_recorded.leader` heartbeat exception). The reaper handles dead-leader
# cleanup automatically via the existing `running` + stale `started_at`
# sweep.
LEADER_KIND = "_recorded.leader"

# Default cadences. Per-Recorder overrides via `Recorder(leader_heartbeat_s=,
# leader_stale_s=)` for tests that need tight cycles.
DEFAULT_LEADER_HEARTBEAT_S = 5.0
DEFAULT_LEADER_STALE_S = 30.0  # 6× heartbeat → 5 missed beats of grace

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
-- supports bare `last()` and `query()` ordered by submitted_at without a kind filter
CREATE INDEX IF NOT EXISTS idx_jobs_submitted_at    ON jobs(submitted_at DESC);

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

INSERT_PENDING = """
INSERT INTO jobs (id, kind, key, status, submitted_at, request_json)
VALUES (?, ?, ?, 'pending', ?, ?)
"""

# Bare-call path inserts directly as 'running' in one atomic write — the
# caller is about to execute the function itself, so there's no `pending`
# state to occupy. Keeps `pending` exclusively as "queued for the leader
# process" and removes the race window where the leader could claim a
# bare-call row.
INSERT_RUNNING = """
INSERT INTO jobs (id, kind, key, status, submitted_at, started_at, request_json)
VALUES (?, ?, ?, 'running', ?, ?, ?)
"""

# Test-only: the leader's CLAIM_ONE handles `pending → running` atomically
# in production; this UPDATE is retained for test fixtures that seed rows
# at a specific status. Not used by any production code path.
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

# Atomic claim: pick the oldest pending row and flip it to running. The outer
# `AND status='pending'` guard turns the UPDATE into a no-op if a competing
# claimant already won, even across processes (WAL serializes the writes).
CLAIM_ONE = f"""
UPDATE jobs
   SET status='running', started_at=?
 WHERE id IN (SELECT id FROM jobs
              WHERE status='pending'
              ORDER BY submitted_at
              LIMIT 1)
   AND status='pending'
RETURNING {", ".join(COLUMNS)}
"""

# Reaper: any row still `running` whose `started_at` predates the threshold
# is presumed orphaned by a dead process. Conditional UPDATE with RETURNING
# id so callers can resolve subscribers waiting on those ids.
#
# Error JSON shape matches the {type, message} convention used by every
# other error writer (_serialize_error, _serialize_recording_failure,
# _CANCEL_ERROR_JSON). Consumers like JoinedSiblingFailedError read .message;
# any other key is silently dropped.
REAP_STUCK = """
UPDATE jobs
   SET status='failed',
       completed_at=?,
       error_json=json_object('type','orphaned','message','process_died_while_running')
 WHERE status='running'
   AND started_at < ?
RETURNING id
"""


def format_iso(dt: datetime) -> str:
    """Render a datetime as our canonical ISO8601 form.

    Naive datetimes are assumed UTC. Aware datetimes are converted.
    Used by the read API to normalize `since=`/`until=` callers passing
    a `datetime` rather than a pre-formatted string.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_iso() -> str:
    """Lex-sortable ISO8601 UTC, microsecond precision, `Z` suffix.

    Used for every timestamp we write.
    """
    return format_iso(datetime.now(timezone.utc))


def new_id() -> str:
    """`uuid4().hex` — unique, no monotonic guarantees."""
    return uuid.uuid4().hex


def open_connection(path: str) -> sqlite3.Connection:
    """Open a connection in WAL mode with sane defaults.

    `check_same_thread=False` because `asyncio.to_thread` calls (and the
    leader's claim/heartbeat threads) use the connection from helper
    threads. Concurrency is serialized at the SQLite level (WAL).
    """
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
