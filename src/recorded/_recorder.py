"""Recorder: owns a SQLite connection and the read methods.

Phase 1.1 ships a skeleton: connection management, schema bootstrap, the
read methods (`get`, `last`), and a module-level lazy default. The write
path (decorator-driven lifecycle) lands in phase 1.2.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from . import _registry, _storage
from ._errors import RecorderClosedError
from ._types import Job


class Recorder:
    """Owns a SQLite connection and exposes read/write primitives.

    For phase 1.1 only the read methods are wired up. `_connection()`,
    schema bootstrap and shutdown are present so 1.2 can plug in the
    write lifecycle without restructuring.
    """

    def __init__(self, path: str = "./jobs.db") -> None:
        self.path = path
        self._lock = threading.Lock()
        # Separate write lock: serialize SQL execution across helper threads
        # (asyncio.to_thread + the test pool). The connection's internal lock
        # is not enough — concurrent execute()s on one connection can race the
        # cursor state and surface as `InterfaceError: bad parameter`.
        self._write_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    # ----- connection lifecycle -----

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise RecorderClosedError(
                    f"Recorder({self.path!r}) has been shut down."
                )
            if self._conn is None:
                self._conn = _storage.open_connection(self.path)
                _storage.ensure_schema(self._conn)
            return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Serialize all execute() calls through `_write_lock`.

        Note: callers that *read* must use `_fetchone` / `_fetchall`
        instead — pulling rows off the returned cursor after the lock has
        been released races concurrent writers and surfaces (rarely) as
        `IndexError` from a partially-stepped cursor.
        """
        conn = self._connection()
        with self._write_lock:
            return conn.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        """Execute + fetchone, both inside `_write_lock`.

        Required for safety under `asyncio.to_thread` concurrency: the
        sqlite3 cursor is bound to a shared connection; stepping it from
        one thread while another thread starts a new execute() on the
        same connection can return a malformed row. The unit suite never
        triggered this; the 50-gathered idempotency test surfaced it
        immediately.
        """
        conn = self._connection()
        with self._write_lock:
            return conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = self._connection()
        with self._write_lock:
            return conn.execute(sql, params).fetchall()

    def shutdown(self) -> None:
        """Idempotent: safe to call repeatedly."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Recorder":
        self._connection()  # eager-bootstrap so errors surface at entry
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    # ----- read API -----

    def get(self, job_id: str) -> Job | None:
        row = self._fetchone(_storage.SELECT_BY_ID, (job_id,))
        if row is None:
            return None
        return _row_to_job(row)

    def last(self, n: int = 10, *, kind: str | None = None) -> list[Job]:
        rows = self._fetchall(_storage.SELECT_LAST, (kind, kind, n))
        return [_row_to_job(r) for r in rows]

    # ----- lifecycle writes -----

    def _insert_pending(
        self,
        job_id: str,
        kind: str,
        key: str | None,
        submitted_at: str,
        request_json: str | None,
    ) -> None:
        self._execute(
            _storage.INSERT_PENDING,
            (job_id, kind, key, submitted_at, request_json),
        )

    def _mark_running(self, job_id: str, started_at: str) -> None:
        self._execute(_storage.UPDATE_RUNNING, (started_at, job_id))

    def _mark_completed(
        self,
        job_id: str,
        completed_at: str,
        response_json: str | None,
        data_json: str | None,
    ) -> None:
        self._execute(
            _storage.UPDATE_COMPLETED,
            (completed_at, response_json, data_json, job_id),
        )

    def _mark_failed(
        self,
        job_id: str,
        completed_at: str,
        error_json: str | None,
    ) -> None:
        self._execute(
            _storage.UPDATE_FAILED, (completed_at, error_json, job_id)
        )

    def _row_status(self, job_id: str) -> str | None:
        row = self._fetchone("SELECT status FROM jobs WHERE id = ?", (job_id,))
        return None if row is None else row[0]

    def _lookup_active_by_kind_key(
        self, kind: str, key: str
    ) -> tuple[str, str] | None:
        """(id, status) of the active row for (kind, key), or None.

        Active = pending/running/completed (the partial-unique-index set).
        Used by the idempotency-collision branch.
        """
        row = self._fetchone(
            "SELECT id, status FROM jobs "
            "WHERE kind=? AND key=? AND status IN ('pending','running','completed')",
            (kind, key),
        )
        return None if row is None else (row[0], row[1])

    def _lookup_latest_failed(
        self, kind: str, key: str
    ) -> str | None:
        row = self._fetchone(
            "SELECT id FROM jobs "
            "WHERE kind=? AND key=? AND status='failed' "
            "ORDER BY submitted_at DESC LIMIT 1",
            (kind, key),
        )
        return None if row is None else row[0]

    def _flush_attach(self, job_id: str, key: str, value: Any) -> None:
        """Write-through path for `attach(..., flush=True)`.

        Merges a single key into `data_json` via `json_patch`, creating
        the object if currently NULL. Used only when `flush=True` is
        passed; the default buffered path writes once at completion.
        """
        payload = json.dumps({key: value})
        self._execute(
            "UPDATE jobs "
            "SET data_json = json_patch(COALESCE(data_json, '{}'), ?) "
            "WHERE id = ?",
            (payload, job_id),
        )


# ----- module-level default singleton -----

_default: Recorder | None = None
_default_lock = threading.Lock()


def get_default() -> Recorder:
    global _default
    with _default_lock:
        if _default is None:
            _default = Recorder()
        return _default


def _set_default(r: Recorder | None) -> None:
    """Test hook: replace the module-level default Recorder."""
    global _default
    with _default_lock:
        if _default is not None and _default is not r:
            _default.shutdown()
        _default = r


# ----- row -> Job rehydration -----


def _loads(s: str | None) -> Any:
    return None if s is None else json.loads(s)


def _row_to_job(row: tuple[Any, ...]) -> Job:
    (
        id_,
        kind,
        key,
        status,
        submitted_at,
        started_at,
        completed_at,
        request_json,
        response_json,
        data_json,
        error_json,
    ) = row
    entry = _registry.get_or_passthrough(kind)
    return Job(
        id=id_,
        kind=kind,
        key=key,
        status=status,
        submitted_at=submitted_at,
        started_at=started_at,
        completed_at=completed_at,
        request=entry.request.deserialize(_loads(request_json)),
        response=entry.response.deserialize(_loads(response_json)),
        data=entry.data.deserialize(_loads(data_json)),
        error=entry.error.deserialize(_loads(error_json)),
    )
