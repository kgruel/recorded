"""Recorder: owns a SQLite connection, the read API, lifecycle SQL, the
wait-primitive notify registry, the stuck-row reaper, and the worker.

Phase 2 Stream A additions over phase 1:

- `_subscribe`/`_resolve` notify primitive used by `JobHandle.wait()` and
  the idempotency-join paths in `_decorator`. Replaces the 5 ms sync-poll.
- Reaper: on Recorder construction, any `running` row whose `started_at`
  predates `reaper_threshold_s` is flipped to `failed` and its subscribers
  resolved.
- Read API: `query()` (filtered iterator), public `connection()` alias of
  `_connection()`, `last(status=...)` for symmetry.
- Atomic-claim helper used by the worker.
- Lazy worker lifecycle (start on first `.submit()`; cancelled and joined
  on `shutdown()`).
"""

from __future__ import annotations

import atexit
import concurrent.futures
import json
import logging
import sqlite3
import threading
import weakref
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
    interpreter's lifetime. If you build a Recorder directly outside a
    `with` block, you are responsible for calling `shutdown()` (or using
    `with` / `async with`) before the interpreter exits — otherwise the
    worker thread can race against interpreter teardown.

    A module-level atexit hook walks live (weak-referenced) Recorders and
    logs a warning for any that started a worker but were never shut down.
    Configured singletons run their `atexit.register(shutdown)` first
    (LIFO order) and arrive at the warning hook with `_closed=True`, so the
    warning targets only the dirty-direct-construction path.

    Use `recorded.configure(path=...)` for the managed-singleton pattern;
    use `with Recorder(path=...) as r:` (or `async with`) for explicit
    instances.
    """

    def __init__(
        self,
        path: str = "./jobs.db",
        *,
        reaper_threshold_s: float = DEFAULT_REAPER_THRESHOLD_S,
        worker_poll_interval_s: float = 0.2,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        warn_on_data_drift: bool = True,
    ) -> None:
        self.path = path
        self.reaper_threshold_s = reaper_threshold_s
        self.worker_poll_interval_s = worker_poll_interval_s
        self.join_timeout_s = join_timeout_s
        self.warn_on_data_drift = warn_on_data_drift
        # Per-Recorder dedup of projection-drift warnings, keyed by
        # `(kind, reason)`. Reset only by constructing a fresh Recorder.
        self._drift_warned: set[tuple[str, str]] = set()

        # `_lock` guards Recorder resources: connection lifecycle (`_conn`,
        # `_closed`) and worker lifecycle (`_worker`). Held briefly; never
        # held across blocking operations (worker.shutdown(), SQL execute).
        self._lock = threading.Lock()
        # `_write_lock` serializes SQL execute+fetch on the shared connection
        # (see PROGRESS_INTEGRATION §3.1: stepping a cursor concurrently with
        # another execute on the same conn returns torn rows).
        self._write_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False

        # Notify registry: job_id -> list of subscribed Futures. Resolved
        # by `_resolve()` after a terminal SQL update commits.
        # `_live_results` rides the same lock — both are consumed in tandem
        # at terminal-resolution time, and using `_lock` for the cache
        # would deadlock since the reaper holds `_lock` while resolving.
        self._notify_lock = threading.Lock()
        self._notify_subscribers: dict[str, list[concurrent.futures.Future]] = {}
        # Live-result cache: in-process leaders stash their live result
        # here before the terminal write so same-process idempotency
        # joiners receive the typed object (not the storage-rehydrated
        # dict). Only stashed for keyed rows. Cleared in `_resolve` on
        # the no-subscriber path; drained by `JobHandle.wait()` so the
        # storage-only consumer doesn't leak the entry.
        self._live_results: dict[str, Any] = {}

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

        # Worker is lazy. Lifecycle (start + shutdown) is under `_lock` so
        # `_closed` and `_worker` live in the same partition — a shutdown
        # in flight can't race a concurrent `_ensure_worker()`.
        self._worker: Any | None = None  # _worker.Worker; avoids circular import

        # Track this instance for the dirty-recorder atexit warning. WeakSet
        # so test suites that build dozens of Recorders don't accumulate
        # callback state — collected instances drop out automatically.
        _LIVE_RECORDERS.add(self)

    # ----- connection lifecycle -----

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            # If the connection is already open, return it even when
            # `_closed=True`. This lets in-flight worker tasks finish their
            # `_mark_failed` writes during shutdown's drain phase. New
            # connection inits are still blocked by `_closed`.
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
        # Atomically: mark closed (blocks new _ensure_worker) and snapshot
        # the worker. The conn stays open during drain so in-flight worker
        # tasks can finalize via `_mark_failed`.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
            self._worker = None
        # Drain. `worker.shutdown()` waits for the worker thread, which
        # needs to call back into the recorder via `_connection()` —
        # released the lock before getting here.
        if worker is not None:
            worker.shutdown()
        # Worker drained. Close the connection.
        with self._lock:
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
        sql = (
            f"SELECT {', '.join(_storage.COLUMNS)} FROM jobs "
            "WHERE (? IS NULL OR kind GLOB ?) "
            "  AND (? IS NULL OR status = ?) "
            "ORDER BY submitted_at DESC "
            "LIMIT ?"
        )
        rows = self._fetchall(sql, (kind, kind, status, status, n))
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
        never occupies `pending`. Worker can never claim it.
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

        Cleans up the live-result cache on the no-subscriber path: if no
        joiner was parked at terminal-write time, no consumer will follow
        and the stash would otherwise leak until the reaper sweeps. Late
        joiners arriving after this point fall through to storage
        rehydration — the documented cross-process trade-off.
        """
        with self._notify_lock:
            lst = self._notify_subscribers.pop(job_id, [])
            if not lst:
                # No joiner parked; drop the stash now to avoid a leak.
                # Late joiners arriving after this point fall through to
                # storage rehydration (the documented cross-process trade).
                self._live_results.pop(job_id, None)
        for fut in lst:
            if not fut.done():
                fut.set_result(status)

    # ----- live-result cache -----

    def _stash_live_result(self, job_id: str, result: Any) -> None:
        """Stash the in-memory result of a successful execution.

        Called by `_run_and_record` (and its async variant) just before
        the terminal write, so same-process idempotency joiners can
        receive the typed object on consume rather than the storage-
        rehydrated dict. Only same-process joiners benefit; cross-process
        joiners always go through storage and need `response=Model` to
        preserve type identity.
        """
        with self._notify_lock:
            self._live_results[job_id] = result

    def _take_live_result(self, job_id: str) -> Any:
        """Pop the live result for `job_id`. Returns `_MISSING` if absent."""
        with self._notify_lock:
            return self._live_results.pop(job_id, _MISSING)

    # ----- reaper -----

    def _reap_stuck_running_unlocked(self, conn: sqlite3.Connection) -> None:
        """Flip orphaned `running` rows to `failed` on Recorder construction.

        Called from `_connection()` immediately after `ensure_schema()`,
        with `self._lock` held. Not gated by `_write_lock` because no
        other thread can hold a connection reference yet — the lazy
        connection has only just been created.

        Also sweeps the live-result cache: any reaped row was orphaned by
        a dead process, so any stash from a prior (now-dead) process is
        gone anyway, but stale entries from the current process for rows
        the reaper just failed are dropped.
        """
        threshold_iso = _storage.format_iso(
            datetime.now(timezone.utc) - timedelta(seconds=self.reaper_threshold_s)
        )
        rows = conn.execute(_storage.REAP_STUCK, (_storage.now_iso(), threshold_iso)).fetchall()
        # Resolve subscribers and clear any straggling stash. `_resolve`
        # already pops `_live_results[job_id]` on the no-subscriber path
        # (which is the path for reaped rows), and uses `_notify_lock` —
        # not `_lock` — so it's safe to call while `_lock` is held here.
        for (job_id,) in rows:
            self._resolve(job_id, _storage.STATUS_FAILED)

    # ----- worker accessors (used by _decorator) -----

    def _ensure_worker(self) -> Any:
        # Construct under `_lock` (same partition as `_closed`). `start()`
        # spawns a thread but does not block on `_lock` — the new thread
        # runs after we release.
        with self._lock:
            if self._closed:
                raise RecorderClosedError(f"Recorder({self.path!r}) has been shut down.")
            if self._worker is None:
                # Lazy import to keep the worker module unloaded for
                # bare-call-only code paths.
                from . import _worker

                self._worker = _worker.Worker(self)
                self._worker.start()
            return self._worker


# ----- helpers -----


# Sentinel for "no cache entry" — distinct from `None` (a valid stashed value).
_MISSING: Any = object()


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


# ----- dirty-recorder atexit warning ----------------------------------------

_LIVE_RECORDERS: weakref.WeakSet[Recorder] = weakref.WeakSet()


def _atexit_warn_dirty_recorders() -> None:
    """Emit a warning at interpreter exit for any live Recorder that started a
    worker but was never shut down. Configure-managed singletons register
    `atexit.register(r.shutdown)` *after* this hook, and atexit runs LIFO, so
    they are shut down first and arrive here with `_closed=True` (silent).

    The hazard surfaced by this warning: a daemon worker thread killed by
    interpreter teardown converts in-flight successes into CancelledError
    rows — see WHY.md / direct-construction docstring.
    """
    for r in list(_LIVE_RECORDERS):
        if r._worker is not None and not r._closed:
            _logger.warning(
                "recorded[%s]: Recorder was constructed directly but never "
                "shut down before interpreter exit. The daemon worker thread "
                "is being killed; in-flight results are recorded as "
                "CancelledError rather than completed. Use `with "
                "Recorder(...) as r:` (or `recorded.configure(...)` for the "
                "managed-singleton pattern) so shutdown is guaranteed.",
                r.path,
            )


atexit.register(_atexit_warn_dirty_recorders)


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
