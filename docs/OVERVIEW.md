# `recorded` — architecture overview

A guided tour of the codebase: what it is, how the pieces fit, and why
the load-bearing decisions are the way they are. For the authoritative
design spec, see `DESIGN.md`. For local-dev setup, see `README.md`.

## The premise

A typed function-call recorder backed by SQLite. You decorate a function
once; every call writes its full lifecycle (request, response, data,
error, timestamps, status) to a SQLite row. Async-native, sync
first-class, FastAPI-friendly, no external runtime deps. The audit log
*is* your queryable history — designed for the case "an agent calls an
LLM/broker/API and you want to ask it later 'what did I do, why, and
when?'"

## The single primitive: the slot

A **slot** is a JSON column on the `jobs` table optionally validated by
a typed model. Four slots:

| slot | purpose | example |
|---|---|---|
| `request` | what the caller asked | `{"prompt": "...", "max_tokens": 100}` |
| `response` | what the function returned | the model's reply object |
| `data` | queryable projection + caller-attached side data | `{"customer_id": 7, "tokens_used": 412}` |
| `error` | failure shape | `{"type": "RateLimitError", "message": "..."}` |

Each slot has a `type | None` adapter declared at decoration. The
adapter (`_adapter.py`) has three modes:

- `passthrough` (`model=None`): the value is JSON-native, or
  auto-rendered if it's a dataclass / pydantic instance.
- `dataclass`: validate by construction, dump via `dataclasses.asdict`.
- `pydantic` (v2, duck-typed): `model_validate` +
  `model_dump(mode="json")`.

The adapter abstraction means `data=Model`, `request=Model`,
`response=Model`, `error=Model` all work the same way — the slot
machinery is uniform across audit roles.

## The schema

One table:

```sql
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,    -- uuid4().hex
  kind          TEXT NOT NULL,       -- "broker.place_order" or auto-derived
  key           TEXT,                -- idempotency key (nullable)
  status        TEXT NOT NULL,       -- pending | running | completed | failed
  submitted_at  TEXT NOT NULL,       -- ISO8601 UTC microsecond Z
  started_at    TEXT,
  completed_at  TEXT,
  request_json  TEXT,
  response_json TEXT,
  data_json     TEXT,
  error_json    TEXT
);

-- Partial unique index for at-most-one-active-per-(kind, key)
CREATE UNIQUE INDEX uq_jobs_kind_key_active ON jobs(kind, key)
  WHERE key IS NOT NULL AND status IN ('pending', 'running', 'completed');
```

The partial index does the load-bearing work for idempotency. Multiple
`failed` rows allowed (history); at most one
`pending`/`running`/`completed` row per `(kind, key)`.

## The lifecycle: three writes

Every recorded call writes three rows-of-state:

```
INSERT pending  →  UPDATE running  →  UPDATE {completed|failed}
```

Same shape for every code path — bare call, submitted call, sync, async.
`Recorder` exposes private write methods (`_insert_pending`,
`_mark_running`, `_mark_completed`, `_mark_failed`); they all serialize
through one `_write_lock` against a single SQLite connection.

## Trace a call: bare path

```python
@recorder(kind="broker.place_order", request=OrderReq, response=OrderReply,
          data=OrderView, error=BrokerError)
async def place_order(req: OrderReq) -> OrderReply:
    ...

result = await place_order(req)
```

What happens (`_decorator.py`):

1. `_validate_call_args(entry, key, args, kwargs)` — refuses misuse:
   `key=` with auto-derived `kind`, `request=Model` with multi-arg call
   shape.
2. `_capture_request(args, kwargs)` — single positional → that arg;
   otherwise → `{"args": [...], "kwargs": {...}}` envelope.
3. `_serialize_request(...)` → JSON via the request adapter.
4. `_insert_pending(job_id, kind, key, submitted_at, request_json)` —
   first write.
5. **`current_job` ContextVar set to a fresh `JobContext`** holding the
   data buffer, the error buffer, and the recorder reference. Crucial —
   this is what `attach()` and `attach_error()` read.
6. `_mark_running(job_id, started_at)` — second write.
7. The wrapped function executes. While running, it can call
   `attach(key, value)` (key-merged into the data buffer via
   `json_patch`) or `attach_error(payload)` (full-replace into the error
   buffer).
8. Branch:
   - **Success**: `_write_completion()` serializes response + data via
     adapters, projects response into data via `_project_response`,
     merges with the attach buffer, calls `_mark_completed(...)` —
     third write. Returns the function's natural value (transparency
     invariant).
   - **Failure**: `_serialize_error()` consults the error buffer (uses
     `error=Model` adapter if `attach_error()` was called; falls back
     to `{type, message}`). Calls `_mark_failed(...)` — third write.
     Re-raises the original exception (wrap-transparency: removing
     `@recorder` doesn't change exception shape).

The async wrapper does all SQLite I/O via `asyncio.to_thread` so the
loop never blocks on disk.

## Trace a call: submitted path

```python
handle = place_order.submit(req, key="order-42")
job = await handle.wait(timeout=30.0)
```

What's different (`_decorator.py::_submit` + `_worker.py`):

1. `_insert_pending(...)` runs in the caller's thread/loop, same as
   bare.
2. **`recorder._ensure_worker()`** — first call lazily spawns the
   `Worker` (one asyncio loop in a dedicated thread; subsequent submits
   reuse it).
3. Returns a `JobHandle(job_id, recorder, kind)` immediately. The
   wrapped function hasn't run yet.
4. The worker's main loop, `_main()`, polls `_claim_one()` — an atomic
   `UPDATE jobs SET status='running' ... WHERE status='pending'
   RETURNING *` that doubles as the multi-process race-safe claim
   primitive (SQLite WAL handles concurrent writers).
5. For each claimed row, the worker spawns a task via
   `asyncio.create_task(_execute(row))`. `_execute` sets `current_job`
   *inside the task* before any await (so the contextvar reaches the
   wrapped function regardless of whether it's coroutine or
   `to_thread`'d). Then runs the function and writes terminal state —
   same `_mark_completed`/`_mark_failed` machinery as the bare path.
6. `handle.wait()` subscribes to the wait primitive
   (`recorder._subscribe(job_id)`) and blocks until the terminal write
   fires `_resolve(job_id, status)`.

The bare path runs the function in the caller's thread. The submitted
path defers it to the worker. Identical row shape on both ends — same
lifecycle invariant.

## The wait primitive — one mechanism, four surfaces

Phase 2's load-bearing refactor. Before it, idempotency-join used a
5 ms sync-poll. After:

```
Recorder._notify_subscribers: dict[job_id, list[concurrent.futures.Future]]
                              guarded by _notify_lock

_subscribe(job_id) → Future:
    register fut under _notify_lock
    check row status (under _write_lock briefly)
    if terminal: pre-resolve fut inline (race-safe via fut.done() guard)
    return fut

_resolve(job_id, status):
    pop subscribers under _notify_lock
    fut.set_result(status) for each
    Called only when cursor.rowcount > 0 in
        _mark_completed / _mark_failed / reaper
```

Used by **four call sites** uniformly:

1. `JobHandle.wait()` / `wait_sync()` — the explicit submit-and-wait
   surface.
2. `_decorator._wait_for_terminal_sync` /
   `_async_wait_for_terminal` — idempotency-collision joins (caller's
   `key=` collided with an active row; wait for it).
3. The cross-process polling fallback — same waiter loop, but if the
   leader is in another process, no in-process `_resolve` ever fires;
   the loop's per-iteration `NOTIFY_POLL_INTERVAL_S` (200 ms) timeout
   doubles as the polling cadence, rechecking row status each tick.
4. The reaper — when a stuck row gets flipped to `failed`, the reaper
   resolves its subscribers so they don't hang waiting for a dead
   leader.

`concurrent.futures.Future` (not `asyncio.Future`) for thread-safety:
one Future serves both `fut.result(timeout=)` for sync waiters and
`asyncio.wrap_future(fut)` for async waiters. Loop-agnostic.

## Idempotency — the partial-unique-index trick

```python
result = await place_order(req, key="order-42")     # sync → response
# OR
handle = place_order.submit(req, key="order-42")    # async → JobHandle
```

The unique index does the work. Flow (sync wrapper, similar in async):

1. **Pre-INSERT lookup**: `_lookup_active_by_kind_key(kind, key)`. If
   found `completed`, return its response. If found `pending`/`running`,
   fall through to wait. If found `failed` and `retry_failed=True`
   (default), proceed to INSERT.
2. **`INSERT pending`**: if it succeeds, we own this `(kind, key)` slot.
   Proceed.
3. **`INSERT pending` raises `IntegrityError`**: lost the race. Look up
   the winning row and wait on it via the wait primitive.

After waiting, `_resolve_terminal(...)`:

- Status == `completed` → return that row's response (this is what the
  caller wanted — same response as if they'd been the leader).
- Status == `failed` → **raise `JoinedSiblingFailedError`** carrying the
  sibling's recorded `error_json`.

The "always raise on failure" rule is the **wrap-transparency
principle**: keyed-call surfaces never return a `Job` representing
failure. Either you get the response, or an exception. To inspect prior
failed rows, use the read API
(`recorded.last(kind=..., key=..., status="failed")`).

`retry_failed=False` extends this — it joins a prior `failed` row and
raises `JoinedSiblingFailedError` rather than retrying. The same
wrap-transparency rule.

## ContextVar plumbing

`current_job: ContextVar[JobContext | None]` lives in `_context.py`.
It's the channel between the wrapper and the wrapped function for
`attach()` / `attach_error()`.

```python
@dataclass
class JobContext:
    job_id: str
    kind: str
    recorder: Recorder
    buffer: dict[str, Any]              # data slot — key-merged
    error_buffer: Any | None = _UNSET   # error slot — full-replace,
                                        # sentinel for "never set"
```

ContextVar (not `threading.local`) because:

- It propagates through `asyncio.gather` task boundaries correctly
  (each task gets its own copy via `Context.copy()`).
- It propagates through `asyncio.to_thread` (Python 3.7+).
- The worker-side `_execute` calls `current_job.set(ctx)` *inside the
  task* before any await, so async functions and `to_thread`'d sync
  functions both see the same ctx.

`attach(key, value)` writes to the data buffer; flushed at completion
(or `flush=True` writes through immediately via `json_patch`).
`attach_error(payload)` writes to `error_buffer` with last-write-wins.
Both raise `AttachOutsideJobError` if called outside an active context.

## The reaper

A defensive sweep for orphaned `running` rows. Runs on
`Recorder._connection()` first-init (i.e., first SQLite touch after
construction):

```sql
UPDATE jobs SET status='failed',
                completed_at=?,
                error_json='{"type":"ReapedAfterCrash",...}'
 WHERE status='running' AND started_at < ?  -- now - threshold
RETURNING id;
```

Default threshold 5 minutes, configurable per-Recorder. Conditional
UPDATE means at-most-once-per-row regardless of who wins across
processes — multiple recorders booting against the same DB don't
double-reap.

Late completion against a reaped row: the original writer's
`UPDATE ... WHERE status='running'` no longer matches (status is now
`failed`). `cursor.rowcount == 0` → no `_resolve()` fires → the reaper's
mark stands.

## The read side

Three surfaces, all on `Recorder`:

| API | shape |
|---|---|
| `recorder.get(job_id) -> Job \| None` | single row by ID |
| `recorder.last(n=10, *, kind=None, status=None) -> list[Job]` | last N, glob on kind |
| `recorder.list(kind, status, key, since, until, where_data, limit, order) -> Iterator[Job]` | filtered iterator with deferred query |
| `recorder.connection() -> sqlite3.Connection` | escape hatch for arbitrary SQL |

`where_data` compiles to `json_extract(data_json, '$.key') = ?` —
equality on top-level keys only, multiple keys AND together. Anything
richer (ranges, aggregations, joins) goes through `connection()` raw
SQL. **No DSL line** — held throughout phase 2.

Module-level `recorded.last/list/get/connection` delegate to the
lazy-default `Recorder` (constructed on first use, configurable via
`recorded.configure(...)`).

`Job` is a dataclass; rehydrates each slot via its registered adapter.
Has `duration_ms` property and `to_prompt()` Markdown serializer for
LLM consumption.

## Lifecycle integration

`recorded.configure(path=..., reaper_threshold_s=..., join_timeout_s=...)`
— configure-once. First call constructs the default `Recorder` and
registers `atexit.register(r.shutdown)`. Subsequent calls are no-ops
(return the existing default).

For FastAPI lifespan, `Recorder` is an async context manager:

```python
@asynccontextmanager
async def lifespan(app):
    async with recorded.configure(path="/var/lib/app/jobs.db") as r:
        yield
```

Bootstraps the connection (and reaper sweep) on enter, shuts down
(drains worker, closes connection) on exit.

`Recorder.shutdown()` is idempotent — atexit + explicit shutdown can
both fire safely.

## CLI

```
python -m recorded last [N] [--kind GLOB] [--status S] [--path P]
python -m recorded get  <job_id> [--prompt] [--path P]
python -m recorded tail [--kind GLOB] [--interval S] [--path P]
```

Stdlib only (argparse). Each invocation builds its own short-lived
`Recorder(path=...)` — never touches the module singleton (would
unnecessarily spawn a worker thread the CLI doesn't use).

`--prompt` on `get` emits `Job.to_prompt()` for paste into an LLM.
`tail` polls `list()` with a moving watermark + boundary-id set for
de-dup.

## The cross-cutting discipline

Three principles tie the library together:

1. **Audit invariant**: the raw response is always recorded unless the
   caller opts into lossy `response=Model`. The passthrough adapter
   auto-renders typed instances to JSON-native form so typed-instance
   returns don't crash recording.
2. **Transparency invariant**: bare call returns the wrapped function's
   natural value. Adding `@recorder` is a recording side-effect, not a
   value transformation.
3. **Wrap-transparency invariant**: keyed-call surfaces (`key=`-routed
   paths, `JobHandle.wait()`) always either succeed or raise. Never
   return a `Job` for failure paths. `JoinedSiblingFailedError` is the
   universal "the row you joined was failed" signal.

Plus the dissolution test as the "should this exist?" gate: can the
proposed feature be expressed as a property of what already exists?
The single-table schema, the slot-with-adapter primitive, the
three-write lifecycle, the one-notify-primitive — these survived
because they passed dissolution. Honker, schema_version, ULID,
dry-run, aggregations didn't.

## File map

```
src/recorded/
  __init__.py     — public surface
  __main__.py     — `python -m recorded` shim
  _adapter.py     — slot adapter (passthrough/dataclass/pydantic)
  _cli.py         — last/get/tail subcommands
  _context.py     — current_job ContextVar + attach() + attach_error()
  _decorator.py   — @recorder + bare-call lifecycle + idempotency join
  _errors.py      — exception hierarchy
  _handle.py      — JobHandle (.wait async + .wait_sync)
  _recorder.py    — Recorder (connection, writes, notify, reaper, configure)
  _registry.py    — kind → RegistryEntry
  _storage.py     — schema DDL, canonical SQL, helpers
  _types.py       — Job dataclass + duration_ms + to_prompt()
  _worker.py      — Worker (asyncio loop in dedicated thread)
  fastapi.py      — capture_request(request) (duck-typed)
```

~1500 LOC of core in 13 files; 136 tests in ~4.5 s.
