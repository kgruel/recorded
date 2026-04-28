# `recorded` — how it works

A guided walk through the codebase: schema, lifecycle, trace a call,
the wait primitive, idempotency mechanics, the reaper, the read side.
Read top-to-bottom for orientation, or jump in by section if you know
what you need.

> **What's in:** the architecture and mechanics of the running library —
> what each subsystem does, how the pieces fit, where things live.
> **What's not:** *why* those choices were made (see [WHY.md](WHY.md)),
> or *how to use* the library (see [usage/](usage/)).

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

## The lifecycle

Bare and submitted calls diverge: `pending` exclusively means
"queued for the leader." Bare-call rows skip `pending` entirely and
insert directly as `running`. The rationale — and the bug class this
structurally eliminates — lives in [WHY.md](WHY.md) under the lifecycle
decisions.

```
bare:        INSERT running                         →  UPDATE {completed|failed}
submitted:   INSERT pending  →  UPDATE running      →  UPDATE {completed|failed}
                                (atomic claim by leader)
```

The bare call inserts a row directly as `running` with `started_at`
populated in the same statement (`INSERT_RUNNING`). The submitted path
inserts as `pending`; the leader process's `CLAIM_ONE` does the atomic
`pending → running` transition. The leader can never see a bare-call
row, which is what makes the leader-double-claiming-bare-call-row class
of race structurally impossible.

`Recorder` exposes private write methods (`_insert_pending`,
`_insert_running`, `_mark_completed`, `_mark_failed`); they all
serialize through one `_write_lock` against a single SQLite connection.

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
4. `_insert_running(job_id, kind, key, submitted_at, started_at,
   request_json)` — first (and only pre-terminal) write. Bare-call rows
   skip `pending` entirely.
5. **`current_job` ContextVar set to a fresh `JobContext`** holding the
   data buffer, the error buffer, and the recorder reference. Crucial —
   this is what `attach()` and `attach_error()` read. Set inside
   `_run_and_record` (`_lifecycle.py`).
6. The wrapped function executes via `_run_and_record(...)`. While
   running, it can call `attach(key, value)` (key-merged into the data
   buffer via `json_patch`) or `attach_error(payload)` (full-replace
   into the error buffer).
7. Branch:
   - **Success**: `_write_completion()` serializes response + data via
     adapters (data auto-projects from response via the data adapter's
     `.project(response)`), merges with the attach buffer, calls
     `_mark_completed(...)` — terminal write. Returns the
     function's natural value (transparency invariant). If
     `_write_completion` itself raises, the row is marked failed, a
     warning is logged on the `recorded` logger, and `result` is still
     returned to the caller — wrap-transparency requires the bare-call
     surface never raise an exception class the wrapped function
     wouldn't.
   - **Failure**: `_serialize_error()` consults the error buffer (uses
     `error=Model` adapter if `attach_error()` was called; falls back
     to `{type, message}` and logs a warning if the typed adapter
     rejects the payload). Calls `_mark_failed(...)` — terminal write.
     Re-raises the original exception (wrap-transparency: removing
     `@recorder` doesn't change exception shape).

The async wrapper does all SQLite I/O via `asyncio.to_thread` so the
loop never blocks on disk.

## Trace a call: submitted path

```python
handle = place_order.submit(req, key="order-42")
job = await handle.wait(timeout=30.0)
```

What's different (`_decorator.py::_submit` + `_cli.py::cmd_run`):

1. `_validate_call_args(...)` — same validation as bare.
2. **Leader-presence gate.** `recorder._is_leader_running()` checks
   for any fresh `_recorded.leader` heartbeat row. If none, `.submit()`
   raises `ConfigurationError` *before* writing any row — loud failure
   on misconfiguration. Bare-call paths do NOT call this check, so
   Tier 1 stays free of leader-detection cost.
3. `_insert_pending(...)` runs in the caller's thread/loop.
4. Returns a `JobHandle(job_id, recorder, kind)` immediately. The
   wrapped function hasn't run yet — it'll run inside the leader process.
5. The leader process (`python -m recorded run --import myapp.tasks`)
   loops on `_claim_one()` — an atomic `UPDATE jobs SET status='running'
   ... WHERE status='pending' RETURNING *` that doubles as the
   multi-process race-safe claim primitive (SQLite WAL handles
   concurrent writers).
6. For each claimed row, the leader spawns an asyncio task that runs
   `_run_and_record_async`. That helper sets `current_job` inside the
   task before any await (so the contextvar reaches the wrapped
   function regardless of whether it's coroutine or `to_thread`'d) and
   writes terminal state via the same `_mark_completed`/`_mark_failed`
   machinery as the bare path.
7. `handle.wait()` subscribes to the wait primitive
   (`recorder._subscribe(job_id)`). Since the writer is in another
   process, the local `_resolve` never fires — the polling fallback
   (200ms cadence via `NOTIFY_POLL_INTERVAL_S`) rechecks `_row_status`
   each tick and resolves when it sees a terminal status.

The bare path runs the function in the caller's process. The submitted
path runs it in the leader's process. Identical row shape on both
ends — same lifecycle invariant.

## The wait primitive — one mechanism, four surfaces

A single subscribe/resolve protocol that serves every "block until this
row reaches terminal status" surface. Sync waiters and async waiters
share one mechanism, loop-agnostic.

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
- The leader's `_execute_claimed_row` calls `current_job.set(ctx)`
  *inside the task* before any await, so async functions and
  `to_thread`'d sync functions both see the same ctx.

`attach(key, value)` writes to the data buffer; flushed at completion
(or `flush=True` writes through immediately via `json_patch`).
`attach_error(payload)` writes to `error_buffer` with last-write-wins.
Both are silent no-ops outside an active context — wrap-transparency:
removing `@recorder` shouldn't require deleting `attach()` calls. (See
[WHY.md](WHY.md) on the wrap-transparency principle.)

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
| `recorder.query(kind, status, key, since, until, where_data, limit, order) -> Iterator[Job]` | filtered iterator with deferred query |
| `recorder.connection() -> sqlite3.Connection` | escape hatch for arbitrary SQL |

`where_data` compiles to `json_extract(data_json, '$.key') = ?` —
equality on top-level keys only, multiple keys AND together. Anything
richer (ranges, aggregations, joins) goes through `connection()` raw
SQL. **No DSL line** — equality-only on top-level keys is the held boundary.

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

Bootstraps the connection (and reaper sweep) on enter, closes the
connection on exit.

`Recorder.shutdown()` is idempotent — atexit + explicit shutdown can
both fire safely.

## Deployment checklist

`recorded` runs in-process against a SQLite WAL database, with cross-
process leadership for `.submit()`. Three operational constraints apply:

- **Same-host storage.** SQLite WAL relies on shared-memory mmap on a
  single host. Network filesystems (NFS, EFS, SMB) are *not* supported by
  SQLite WAL — concurrent access leads to corruption or invisible lock
  collisions that the test suite cannot reproduce. Keep `jobs.db` on local
  disk. See [sqlite.org/wal.html](https://sqlite.org/wal.html).
- **Cross-process clock skew bounds reaper accuracy.** The reaper flips
  rows still in `running` whose `started_at` predates `now() -
  reaper_threshold_s` (default 5 min). Each process uses its own clock; if
  process clocks drift more than `reaper_threshold_s` apart, one process
  can spurious-reap another's in-flight rows. Containers and CI runners
  typically drift 50–500 ms under steady NTP, but resync events can spike
  to seconds. For multi-process deployments on shared infra: keep NTP
  healthy, or set `reaper_threshold_s` higher than the worst expected
  skew.
- **`.submit()` requires a leader process.** Run `python -m recorded run
  --path <jobs.db> --import <module>` as a long-lived sibling process
  (one per physical or logical worker). `.submit()` raises
  `ConfigurationError` if no leader heartbeat is fresh; deployments
  without a leader fail loudly on first submit. Probe via
  `recorder.is_leader_running()` for health checks. See
  [usage/workers.md](usage/workers.md).

## Warnings policy

The library emits two distinct categories of warning, surfaced through
different channels:

- **Lifecycle / usage hazards** — "your code is doing something the
  library can't safely handle." Examples: leader-heartbeat resurrection
  (was reaped past the threshold, re-claimed slot), leader shutdown-
  drain timeout. These emit BOTH:
  - `_logger.warning(...)` on the `recorded` logger (covers structured-
    logging deployments where stderr isn't routed)
  - `warnings.warn(msg, recorded.RecordedWarning, stacklevel=...)`
    (covers test-discipline users running `pytest -W error::recorded.RecordedWarning`,
    and surfaces at interactive prompts)

- **Informational telemetry** — "an observation you may want to act on,
  not necessarily misuse." Example: data-projection drift warnings.
  These emit logger-only — they are observations, not signals to fix
  the code path that triggered them.

`recorded.RecordedWarning` is a `RuntimeWarning` subclass; users who
want to treat all library warnings as errors can:

```python
import warnings, recorded
warnings.filterwarnings("error", category=recorded.RecordedWarning)
```

…or via the pytest CLI:

```
pytest -W error::recorded.RecordedWarning
```

Future hazard sites should follow this dual-channel convention; new
informational telemetry should stay logger-only.

## CLI

```
python -m recorded last [N] [--kind GLOB] [--status S] [--path P]
python -m recorded get  <job_id> [--prompt] [--path P]
python -m recorded tail [--kind GLOB] [--interval S] [--path P]
python -m recorded run  [--path P] [--import MOD ...] [--shutdown-timeout S]
```

Stdlib only (argparse). Each invocation builds its own short-lived
`Recorder(path=...)` — never touches the module singleton.

`run` is the leader process. It claims a `_recorded.leader` heartbeat
row keyed by `host:pid`, refreshes it periodically, loops on
`_claim_one()` to execute pending rows. SIGTERM/SIGINT triggers
graceful drain (bounded by `--shutdown-timeout`) and DELETEs the
heartbeat row. See [usage/workers.md](usage/workers.md).

`--prompt` on `get` emits `Job.to_prompt()` for paste into an LLM.
`tail` polls `query()` with a moving watermark + boundary-id set for
de-dup. Reads filter out `_recorded.*` heartbeat rows by default;
pass `--kind '_recorded.*'` to inspect them.

## Three invariants the runtime preserves

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

The reasoning behind these — and the dissolution test that decided what
survived to become these invariants — lives in [WHY.md](WHY.md).

## File map

```
src/recorded/
  __init__.py     — public surface
  __main__.py     — `python -m recorded` shim
  _adapter.py     — Adapter ABC + Passthrough/Dataclass/Pydantic concrete subclasses
  _cli.py         — last/get/tail/run subcommands; cmd_run is the leader process
  _context.py     — current_job ContextVar + attach() + attach_error()
  _decorator.py   — @recorder + bare-call wrappers + .submit (with leader gate)
  _errors.py      — exception hierarchy + RecordedWarning
  _handle.py      — JobHandle + canonical _wait_for_terminal_{sync,async} helpers
  _lifecycle.py   — _run_and_record + _validate_call_args + _serialize_*
                    + _write_completion (recording machinery shared by bare + leader)
  _recorder.py    — Recorder (connection, writes, notify, reaper,
                    leader heartbeat protocol, configure)
  _registry.py    — kind → RegistryEntry
  _storage.py     — schema DDL, canonical SQL, helpers, LEADER_KIND constant
  _types.py       — Job dataclass + duration_ms + to_prompt()
  fastapi.py      — capture_request(request) (duck-typed)
```

~3700 LOC of core in 15 files; 167 tests.
