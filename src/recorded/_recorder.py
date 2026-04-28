"""Recorder: owns a SQLite connection, the read API, lifecycle SQL, the
wait-primitive notify registry, the stuck-row reaper, and the leader
heartbeat protocol.

- `_subscribe`/`_resolve` notify primitive used by `JobHandle.wait()` and
  the idempotency-join paths in `_decorator`. Replaces the 5 ms sync-poll.
- Reaper: on Recorder construction, any `running` row whose `started_at`
  predates `reaper_threshold_s` is flipped to `failed` and its subscribers
  resolved.
- Read API: `query()` (filtered iterator), public `connection()` alias of
  `_connection()`, `last(status=...)` for symmetry.
- Atomic-claim helper used by the leader process.
- Leader heartbeat: `_claim_leader_slot`/`_touch_leader_heartbeat`/
  `is_leader_running` for the cross-process `.submit()` model.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from . import _registry, _storage
from ._errors import ConfigurationError, RecorderClosedError
from ._types import Job, _parse_iso

_logger = logging.getLogger("recorded")

# Default join timeout for `JobHandle.wait()` / `wait_sync()`. Stream C
# wires this through `recorded.configure(join_timeout_s=...)` and a per-call
# argument; until then it's a module constant.
DEFAULT_JOIN_TIMEOUT_S = 30.0

# How often the wait helper rechecks `_row_status` while a future is pending.
# Cross-process leaders never resolve our local future, so the recheck is
# the cross-process polling fallback. In-process the future fires inside
# the first `result(timeout=)` call and we never run a second iteration.
NOTIFY_POLL_INTERVAL_S = 0.2

# Default reaper threshold: 5 minutes. Configurable per-Recorder via the
# `reaper_threshold_s=` keyword. Per-kind override is design-for-future.
DEFAULT_REAPER_THRESHOLD_S = 5 * 60.0


class Recorder:
    """Owns a SQLite connection and exposes read/write primitives.

    Direct construction (`Recorder(path=...)`) does NOT register
    `atexit.register(self.shutdown)` — only `recorded.configure(...)` does.
    Test fixtures and explicit-instance code paths construct dozens of
    Recorders; auto-registering each would leak `atexit` callbacks for the
    interpreter's lifetime. Use `with Recorder(path=...) as r:` (or `async
    with`) for explicit instances; `recorded.configure(path=...)` for the
    managed-singleton pattern.

    `.submit()` requires a leader process (`python -m recorded run --path
    <jobs.db>`) to claim and execute pending rows; `.submit()` raises
    `ConfigurationError` if no leader heartbeat is fresh. Probe with
    `recorder.is_leader_running()`.
    """

    def __init__(
        self,
        path: str = "./jobs.db",
        *,
        reaper_threshold_s: float = DEFAULT_REAPER_THRESHOLD_S,
        worker_poll_interval_s: float = 0.2,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        warn_on_data_drift: bool = True,
        leader_heartbeat_s: float = _storage.DEFAULT_LEADER_HEARTBEAT_S,
        leader_stale_s: float = _storage.DEFAULT_LEADER_STALE_S,
    ) -> None:
        # Threshold ordering invariant — loud failure on misconfiguration.
        # The leader heartbeats every `leader_heartbeat_s`; a fresh row is
        # one whose `started_at` is within `leader_stale_s`; the reaper
        # flips to `failed` only after `reaper_threshold_s`. Any reorder
        # produces silent footguns: heartbeat ≥ stale → `is_leader_running`
        # flickers False between refreshes and `.submit()` raises spuriously;
        # stale ≥ reaper → the reaper kills our row before we'd notice it
        # was stale, defeating the resurrection re-claim path.
        if not (leader_heartbeat_s < leader_stale_s < reaper_threshold_s):
            raise ConfigurationError(
                "Recorder threshold ordering violated: must satisfy "
                "leader_heartbeat_s < leader_stale_s < reaper_threshold_s. "
                f"Got leader_heartbeat_s={leader_heartbeat_s}, "
                f"leader_stale_s={leader_stale_s}, "
                f"reaper_threshold_s={reaper_threshold_s}."
            )

        self.path = path
        self.reaper_threshold_s = reaper_threshold_s
        self.worker_poll_interval_s = worker_poll_interval_s
        self.join_timeout_s = join_timeout_s
        self.warn_on_data_drift = warn_on_data_drift
        self.leader_heartbeat_s = leader_heartbeat_s
        self.leader_stale_s = leader_stale_s
        # Per-Recorder dedup of projection-drift warnings, keyed by
        # `(kind, reason)`. Reset only by constructing a fresh Recorder.
        self._drift_warned: set[tuple[str, str]] = set()

        # `_lock` guards Recorder connection lifecycle (`_conn`, `_closed`).
        # Held briefly; never held across blocking operations (SQL execute).
        self._lock = threading.Lock()
        # `_write_lock` serializes SQL execute+fetch on the shared connection
        # (see PROGRESS_INTEGRATION §3.1: stepping a cursor concurrently with
        # another execute on the same conn returns torn rows).
        self._write_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False

        # Notify registry: job_id -> list of subscribed Futures. Resolved
        # by `_resolve()` after a terminal SQL update commits.
        self._notify_lock = threading.Lock()
        self._notify_subscribers: dict[str, list[concurrent.futures.Future]] = {}

        # Test-only callback fired from `_subscribe` once the future is
        # registered (whether it parks or pre-resolves inline). Used by
        # tests that need to coordinate with the moment a waiter is
        # parked on a row, without sleeping for "give wait() a chance"
        # — the only consumer is the test suite, marked `_for_testing_*`
        # to match `_set_default_for_testing`.
        #
        # Fires once per `_subscribe` call. If a test path can have multiple
        # subscribers on the same row (e.g. `JobHandle.wait()` + idempotency
        # joiner), the callback should filter by `job_id` rather than just
        # signalling on any subscribe.
        self._for_testing_subscribe_callback: Callable[[str], None] | None = None

    # ----- connection lifecycle -----

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            # If the connection is already open, return it even when
            # `_closed=True`. This lets in-flight async/background tasks
            # finish their `_mark_failed` writes during shutdown's drain
            # phase (e.g. the leader process draining its in-flight
            # `_execute_claimed_row` tasks before exit). New connection
            # inits are still blocked by `_closed`.
            if self._conn is not None:
                return self._conn
            if self._closed:
                raise RecorderClosedError(f"Recorder({self.path!r}) has been shut down.")
            self._conn = _storage.open_connection(self.path)
            _storage.ensure_schema(self._conn)
            self._reap_stuck_running_unlocked(self._conn)
            return self._conn

    # Public alias of the connection accessor. WHY.md names
    # `recorded.connection()` (and `Recorder.connection()`) as the escape
    # hatch for raw SQL. `_connection` stays as a private alias since the
    # phase-1 test suite reaches into it directly; touching those test
    # sites is out of Stream A's scope.
    def connection(self) -> sqlite3.Connection:
        return self._connection()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        conn = self._connection()
        with self._write_lock:
            return conn.execute(sql, params)

    def _execute_count(self, sql: str, params: tuple = ()) -> int:
        """Execute and return rowcount under the write lock.

        Used for conditional UPDATEs whose follow-up action depends on
        whether any row matched (e.g. notify-resolve only on real terminal
        transitions).
        """
        conn = self._connection()
        with self._write_lock:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def _fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
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

    def __enter__(self) -> Recorder:
        self._connection()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    async def __aenter__(self) -> Recorder:
        # Bootstrap the connection on the threadpool so the loop stays
        # responsive during the first-touch reaper sweep.
        import asyncio

        await asyncio.to_thread(self._connection)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        import asyncio

        await asyncio.to_thread(self.shutdown)

    # ----- read API -----

    def get(self, job_id: str) -> Job | None:
        row = self._fetchone(_storage.SELECT_BY_ID, (job_id,))
        if row is None:
            return None
        return _row_to_job(row)

    def last(
        self,
        n: int = 10,
        *,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[Job]:
        # Reserved-kind exclusion: when `kind` is unspecified, hide library-
        # internal rows (`_recorded.*`, e.g. leader heartbeats) so users
        # browsing the audit log don't see them. Explicit `kind="_recorded.*"`
        # opts in.
        clauses = [
            "(? IS NULL OR kind GLOB ?)",
            "(? IS NULL OR status = ?)",
        ]
        params: list[Any] = [kind, kind, status, status]
        if kind is None:
            clauses.append("kind NOT GLOB ?")
            params.append(_storage.RESERVED_KIND_GLOB)
        sql = (
            f"SELECT {', '.join(_storage.COLUMNS)} FROM jobs "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY submitted_at DESC "
            "LIMIT ?"
        )
        params.append(n)
        rows = self._fetchall(sql, tuple(params))
        return [_row_to_job(r) for r in rows]

    def query(
        self,
        kind: str | None = None,
        status: str | Sequence[str] | None = None,
        key: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        where_data: dict[str, Any] | None = None,
        limit: int = 100,
        order: str = "desc",
    ) -> Iterator[Job]:
        """Filtered iterator over jobs. Query is deferred until first `next()`.

        `kind` accepts a glob (`broker.*`). `status` accepts a single string
        (`"completed"`) or a tuple/list (`("completed", "failed")`) — useful
        for tail-style consumers that want all terminal rows in one query.
        `where_data` is equality on top-level keys of `data_json` only —
        multiple keys AND together. Anything richer goes through
        `connection()` and raw SQL.

        The iterator buffers all matched rows internally on first `next()`
        rather than streaming a live cursor: holding the cursor across
        thread boundaries races concurrent writes (PROGRESS_INTEGRATION
        §3.1). "Lazy" here means "query deferred", not "row-streamed".
        """
        order_norm = order.lower()
        if order_norm not in ("asc", "desc"):
            raise ConfigurationError(f"query(order=...) must be 'asc' or 'desc', got {order!r}")

        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind GLOB ?")
            params.append(kind)
        else:
            # Reserved-kind exclusion: hide `_recorded.*` rows when the caller
            # didn't ask for a specific kind. Explicit `kind="_recorded.*"`
            # opts in (the branch above runs instead).
            clauses.append("kind NOT GLOB ?")
            params.append(_storage.RESERVED_KIND_GLOB)
        if status is not None:
            if isinstance(status, str):
                clauses.append("status = ?")
                params.append(status)
            else:
                statuses = tuple(status)
                if not statuses:
                    raise ConfigurationError("query(status=...) tuple/list must be non-empty.")
                placeholders = ",".join("?" * len(statuses))
                clauses.append(f"status IN ({placeholders})")
                params.extend(statuses)
        if key is not None:
            clauses.append("key = ?")
            params.append(key)
        if since is not None:
            clauses.append("submitted_at >= ?")
            params.append(_normalize_iso(since))
        if until is not None:
            clauses.append("submitted_at <= ?")
            params.append(_normalize_iso(until))
        if where_data:
            for k, v in where_data.items():
                if "." in k or "$" in k:
                    raise ConfigurationError(
                        f"where_data key {k!r} contains '.' or '$'; "
                        "only top-level equality is supported. Use "
                        "recorded.connection() for richer queries."
                    )
                clauses.append("json_extract(data_json, ?) = ?")
                params.append(f"$.{k}")
                params.append(v)

        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT {', '.join(_storage.COLUMNS)} FROM jobs"
            f"{where_sql} ORDER BY submitted_at {order_norm.upper()} LIMIT ?"
        )
        params.append(limit)

        # Deferred-query generator: query fires on first `next()`.
        def _gen() -> Iterator[Job]:
            rows = self._fetchall(sql, tuple(params))
            for r in rows:
                yield _row_to_job(r)

        return _gen()

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

    def _insert_running(
        self,
        job_id: str,
        kind: str,
        key: str | None,
        submitted_at: str,
        started_at: str,
        request_json: str | None,
    ) -> None:
        """Bare-call insert: row enters as `running` in one write.

        The caller is about to execute the function itself, so the row
        never occupies `pending`. The leader's `_claim_one` only matches
        `pending` rows, so it can't claim a bare-call row.
        """
        self._execute(
            _storage.INSERT_RUNNING,
            (job_id, kind, key, submitted_at, started_at, request_json),
        )

    def _mark_completed(
        self,
        job_id: str,
        completed_at: str,
        response_json: str | None,
        data_json: str | None,
    ) -> None:
        rowcount = self._execute_count(
            _storage.UPDATE_COMPLETED,
            (completed_at, response_json, data_json, job_id),
        )
        if rowcount > 0:
            self._resolve(job_id, _storage.STATUS_COMPLETED)

    def _mark_failed(
        self,
        job_id: str,
        completed_at: str,
        error_json: str | None,
    ) -> None:
        rowcount = self._execute_count(_storage.UPDATE_FAILED, (completed_at, error_json, job_id))
        if rowcount > 0:
            self._resolve(job_id, _storage.STATUS_FAILED)

    def _claim_one(self) -> tuple | None:
        """Atomic pending → running transition; returns the claimed row."""
        conn = self._connection()
        with self._write_lock:
            row = conn.execute(_storage.CLAIM_ONE, (_storage.now_iso(),)).fetchone()
        return row

    def _row_status(self, job_id: str) -> str | None:
        row = self._fetchone("SELECT status FROM jobs WHERE id = ?", (job_id,))
        return None if row is None else row[0]

    def _row_error_dict(self, job_id: str) -> dict | None:
        """Raw `error_json` decoded as a dict (no model rehydration).

        Used by `JoinedSiblingFailedError.sibling_error`, which must surface
        the unconverted recorded shape regardless of whether `error=Model`
        rehydrates `Job.error` into an instance on the read path.
        """
        row = self._fetchone("SELECT error_json FROM jobs WHERE id = ?", (job_id,))
        if row is None or row[0] is None:
            return None
        try:
            decoded = json.loads(row[0])
        except Exception:
            return None
        return decoded if isinstance(decoded, dict) else None

    def _lookup_active_by_kind_key(self, kind: str, key: str) -> tuple[str, str] | None:
        row = self._fetchone(
            "SELECT id, status FROM jobs "
            "WHERE kind=? AND key=? AND status IN ('pending','running','completed')",
            (kind, key),
        )
        return None if row is None else (row[0], row[1])

    def _lookup_latest_failed(self, kind: str, key: str) -> str | None:
        row = self._fetchone(
            "SELECT id FROM jobs "
            "WHERE kind=? AND key=? AND status='failed' "
            "ORDER BY submitted_at DESC LIMIT 1",
            (kind, key),
        )
        return None if row is None else row[0]

    def _flush_attach(self, job_id: str, key: str, value: Any) -> None:
        payload = json.dumps({key: value})
        self._execute(
            "UPDATE jobs SET data_json = json_patch(COALESCE(data_json, '{}'), ?) WHERE id = ?",
            (payload, job_id),
        )

    # ----- notify primitive -----

    def _subscribe(self, job_id: str) -> concurrent.futures.Future:
        """Register a Future that resolves to the terminal status of `job_id`.

        If the row is already terminal at subscription time, the future is
        pre-resolved before return — callers never need a special "is it
        already done?" check.

        Race-safety: the future is registered under `_notify_lock` BEFORE
        the status check so a concurrent writer's `_resolve` call either
        (a) sees us in the subscriber list and resolves us, or (b) wrote
        terminal and committed before our status check, in which case our
        status check finds it.
        """
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._notify_lock:
            self._notify_subscribers.setdefault(job_id, []).append(fut)

        # Now check status. If already terminal, resolve inline and detach
        # ourselves from the subscriber list so a later `_resolve` doesn't
        # re-set (which would no-op anyway, but the cleanup is cheap).
        status = self._row_status(job_id)
        if status in _storage.TERMINAL_STATUSES:
            with self._notify_lock:
                lst = self._notify_subscribers.get(job_id)
                if lst is not None:
                    try:
                        lst.remove(fut)
                    except ValueError:
                        pass
                    if not lst:
                        self._notify_subscribers.pop(job_id, None)
            if not fut.done():
                fut.set_result(status)
        if self._for_testing_subscribe_callback is not None:
            self._for_testing_subscribe_callback(job_id)
        return fut

    def _unsubscribe(self, job_id: str, fut: concurrent.futures.Future) -> None:
        with self._notify_lock:
            lst = self._notify_subscribers.get(job_id)
            if lst is None:
                return
            try:
                lst.remove(fut)
            except ValueError:
                pass
            if not lst:
                self._notify_subscribers.pop(job_id, None)

    def _resolve(self, job_id: str, status: str) -> None:
        """Notify all subscribers waiting on `job_id` of its terminal status.

        Must be called only after the terminal SQL UPDATE commits — the
        callers (`_mark_completed`, `_mark_failed`, the reaper) gate on
        `cursor.rowcount > 0` so a no-op UPDATE (e.g. a late completion
        for a reaped row) doesn't double-resolve subscribers that another
        writer already handled.
        """
        with self._notify_lock:
            lst = self._notify_subscribers.pop(job_id, [])
        for fut in lst:
            if not fut.done():
                fut.set_result(status)

    # ----- reaper -----

    def _reap_stuck_running_unlocked(self, conn: sqlite3.Connection) -> None:
        """Flip orphaned `running` rows to `failed` on Recorder construction.

        Called from `_connection()` immediately after `ensure_schema()`,
        with `self._lock` held. Not gated by `_write_lock` because no
        other thread can hold a connection reference yet — the lazy
        connection has only just been created.
        """
        threshold_iso = _storage.format_iso(
            datetime.now(timezone.utc) - timedelta(seconds=self.reaper_threshold_s)
        )
        rows = conn.execute(_storage.REAP_STUCK, (_storage.now_iso(), threshold_iso)).fetchall()
        # `_resolve` uses `_notify_lock` — not `_lock` — so it's safe to
        # call while `_lock` is held here.
        for (job_id,) in rows:
            self._resolve(job_id, _storage.STATUS_FAILED)

    # ----- leader heartbeat (cross-process leadership) -----
    #
    # Heartbeats live as `_recorded.leader` rows in the `jobs` table — see
    # `_storage.LEADER_KIND` and WHY.md::Lifecycle (the heartbeat exception:
    # these rows stay `running` indefinitely while their `started_at` is
    # updated periodically). The reaper handles dead-leader cleanup
    # automatically: a stale `running` row with old `started_at` gets flipped
    # to `failed` on the next `_connection()` boot, and `is_leader_running()`
    # then sees no fresh row.

    def _claim_leader_slot(self, host_pid: str) -> str:
        """Insert a fresh leader heartbeat row keyed by `host_pid`.

        Returns the new row's job id. If a prior heartbeat row from the same
        `(LEADER_KIND, host_pid)` is still active (e.g. our process restarted
        before the reaper cleaned it out), DELETE it and re-INSERT. Different
        `(host, pid)` pairs coexist — the partial unique index forbids
        collisions only within the same key.
        """
        job_id = _storage.new_id()
        now = _storage.now_iso()
        try:
            self._insert_running(job_id, _storage.LEADER_KIND, host_pid, now, now, None)
        except sqlite3.IntegrityError:
            # Stale heartbeat row from same host:pid (e.g. process restart
            # before reaper sweep). Hard-delete and retry — the prior row
            # was definitely abandoned because we are the same host:pid.
            conn = self._connection()
            with self._write_lock:
                conn.execute(
                    "DELETE FROM jobs WHERE kind=? AND key=? "
                    "AND status IN ('pending', 'running', 'completed')",
                    (_storage.LEADER_KIND, host_pid),
                )
            self._insert_running(job_id, _storage.LEADER_KIND, host_pid, now, now, None)
        return job_id

    def _touch_leader_heartbeat(self, leader_id: str) -> bool:
        """Refresh `started_at` on a leader heartbeat row. Returns True if
        the row is still ours (i.e. the conditional UPDATE matched).

        Deliberately bypasses `_resolve()`: heartbeat rows have no
        subscribers — they are liveness signals, not jobs. The conditional
        `status='running'` guard means a reaper-flipped row no-ops here;
        the leader's caller checks the return and re-claims a new slot if
        False (resurrection edge per PLAN.md §6.3).
        """
        rowcount = self._execute_count(
            "UPDATE jobs SET started_at=? WHERE id=? AND status='running'",
            (_storage.now_iso(), leader_id),
        )
        return rowcount > 0

    def _release_leader_slot(self, host_pid: str) -> None:
        """DELETE this process's running heartbeat row on graceful shutdown.

        Keyed on `host_pid` (process identity), not the row id from
        `_claim_leader_slot`: the resurrection branch in `_heartbeat_loop`
        rebinds `leader_id` after a reap, so an id-keyed release would
        leave the resurrected row alive. `host_pid` is process-stable.

        Assumes one running slot per `host_pid` — enforced by the partial
        unique index on `(kind, key)` excluding failed, and relied on by
        `_claim_leader_slot`'s collision branch. If that invariant ever
        relaxes (multi-slot-per-process), switch back to id-keying.

        The `status='running'` guard preserves prior reaper-flipped rows
        as audit evidence; only the live slot is removed. Crash-mode
        cleanup falls back to the reaper.
        """
        conn = self._connection()
        with self._write_lock:
            conn.execute(
                "DELETE FROM jobs WHERE kind=? AND key=? AND status='running'",
                (_storage.LEADER_KIND, host_pid),
            )

    def _is_leader_running(self) -> bool:
        """Cheap probe: any fresh `_recorded.leader` row?

        "Fresh" means `started_at >= now() - leader_stale_s`. The reaper
        threshold is independent (typically 10× larger): staleness fails
        fast for `.submit()` gating; the reaper only cleans up genuinely
        dead processes.
        """
        threshold_iso = _storage.format_iso(
            datetime.now(timezone.utc) - timedelta(seconds=self.leader_stale_s)
        )
        row = self._fetchone(
            "SELECT 1 FROM jobs WHERE kind=? AND status='running' AND started_at >= ? LIMIT 1",
            (_storage.LEADER_KIND, threshold_iso),
        )
        return row is not None

    def is_leader_running(self) -> bool:
        """Public probe: is a leader process actively claiming jobs against
        this Recorder's database?

        Returns True if any `_recorded.leader` heartbeat row was refreshed
        within `leader_stale_s` (default 30s). Use this in deployment
        health checks before issuing `.submit()` calls — `.submit()` raises
        `ConfigurationError` when this returns False.
        """
        return self._is_leader_running()


# ----- helpers -----


def _normalize_iso(value: str | datetime) -> str:
    """Canonicalize a since=/until= argument to the stored timestamp format.

    Strings are parsed and reformatted so non-canonical inputs (no microseconds,
    no Z suffix) lex-compare correctly against canonical stored values. A bad
    string surfaces as `ConfigurationError` rather than a silent off-by-one.
    """
    if isinstance(value, str):
        try:
            parsed = _parse_iso(value)
        except ValueError as e:
            raise ConfigurationError(f"since=/until= got non-ISO8601 string {value!r}: {e}") from e
        return _storage.format_iso(parsed)
    return _storage.format_iso(value)


# ----- module-level default singleton -----

_default: Recorder | None = None
_default_lock = threading.Lock()
_atexit_registered_for: set[int] = set()


def get_default() -> Recorder:
    global _default
    with _default_lock:
        if _default is None:
            _default = Recorder()
        return _default


def _set_default_for_testing(r: Recorder | None) -> None:
    """Test-only swap of the module-level default Recorder. **Not for
    production use** — the explicit name is the contract.

    The previous default (if any and not the same instance) is shut down.
    Direct construction via `Recorder(...)` does NOT register `atexit` —
    only the *configured* default does (see `configure()`); test fixtures
    that swap via this hook are responsible for their own teardown.

    The synchronous shutdown means a production caller swapping the default
    mid-flight would tear down the recorder under concurrent users.
    """
    global _default
    with _default_lock:
        if _default is not None and _default is not r:
            _default.shutdown()
        _default = r


def configure(
    path: str | None = None,
    *,
    reaper_threshold_s: float | None = None,
    worker_poll_interval_s: float | None = None,
    join_timeout_s: float | None = None,
    warn_on_data_drift: bool | None = None,
    leader_heartbeat_s: float | None = None,
    leader_stale_s: float | None = None,
) -> Recorder:
    """Configure the module-level default `Recorder`. Configure-once.

    The first call constructs a `Recorder` with the given keywords (only
    those explicitly passed are forwarded; `None` values fall back to
    `Recorder.__init__` defaults), installs it as the module default, and
    registers `atexit.register(recorder.shutdown)` so the connection
    closes cleanly when the interpreter exits.

    Subsequent calls are no-ops: they return the existing default
    unchanged. Tests and explicit-instance code paths bypass this by
    constructing `Recorder(...)` directly.

    Returns the default `Recorder`. Composes with `async with`:

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            async with recorded.configure(path="...") as recorder:
                yield
    """
    global _default
    with _default_lock:
        if _default is not None:
            return _default
        kwargs: dict[str, Any] = {}
        if path is not None:
            kwargs["path"] = path
        if reaper_threshold_s is not None:
            kwargs["reaper_threshold_s"] = reaper_threshold_s
        if worker_poll_interval_s is not None:
            kwargs["worker_poll_interval_s"] = worker_poll_interval_s
        if join_timeout_s is not None:
            kwargs["join_timeout_s"] = join_timeout_s
        if warn_on_data_drift is not None:
            kwargs["warn_on_data_drift"] = warn_on_data_drift
        if leader_heartbeat_s is not None:
            kwargs["leader_heartbeat_s"] = leader_heartbeat_s
        if leader_stale_s is not None:
            kwargs["leader_stale_s"] = leader_stale_s
        r = Recorder(**kwargs)
        _default = r
        # Only the *configured* default registers atexit — direct
        # `Recorder(...)` construction is too noisy (tests build dozens).
        # Guard against double-registration if the same Recorder somehow
        # reaches this branch twice (it shouldn't under configure-once).
        if id(r) not in _atexit_registered_for:
            _atexit_registered_for.add(id(r))
            atexit.register(r.shutdown)
        return r


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
        # Errors may have been recorded under the default `{type, message}`
        # fallback shape even when `error=Model` is registered (e.g. the
        # wrapped function never called `attach_error()`, or its payload
        # failed validation). Rehydration through the model would crash;
        # fall back to the raw dict.
        error=_safe_deserialize(entry.error, _loads(error_json)),
    )


def _safe_deserialize(adapter: Any, raw: Any) -> Any:
    try:
        return adapter.deserialize(raw)
    except Exception:
        return raw
