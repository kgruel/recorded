# recorded — design notes

Package: `recorded` (PyPI). Decorator: `recorder`. Class: `Recorder`. Status: design-stage, pre-code.

## What it is

A typed function-call recorder backed by SQLite, async-native with first-class sync support. You wrap a callable; every invocation goes through the same lifecycle (`pending → running → terminal`) and produces a structured row with request, response, timing, status, and an optional typed projection. Bare calls and submitted-and-polled calls share the same data model; the only difference is who drives the lifecycle.

It is **not** an audit logger in the row-mutation sense, **not** a workflow engine, **not** a task queue with persistence as an incidental feature. The persistent typed record *is* the product.

## The primitive

A "slot" is a JSON column that may optionally be validated by a typed model on write and rehydrated as a model on read. Defaults to "capture whatever was given/returned." Each slot is independently typeable. That is the entire conceptual surface; everything else is sugar over it.

Type adapter tiers (in priority order, all supported in v1):
- **stdlib `dataclasses`** — first-class, no extra deps. Adapter uses `__init__` + `dataclasses.asdict`.
- **Pydantic** — auto-detected if installed; `model_validate` / `model_dump`. No import unless present.
- **plain dict / primitive** — always accepted, stored as-is, no validation.

Core has zero required dependencies beyond the stdlib.

## Public API

```python
from recorded import recorder, Recorder, attach
import recorded

# async-decorated (the natural FastAPI shape)
@recorder(kind="broker.place_order", data=OrderView)
async def place_order(req: PlaceOrder) -> dict:
    async with httpx.AsyncClient() as c:
        return (await c.post(url, json=req)).json()

# sync-decorated — also first-class
@recorder(kind="db.fetch_position")
def fetch_position(req): ...

# === calling: bare call preserves the function's calling convention ===
result = await place_order(req)        # async fn from async caller — natural
result = fetch_position(req)           # sync fn from sync caller — natural

# === cross-mode shims ===
result = place_order.sync(req)         # sync caller invoking async fn
result = await fetch_position.async_run(req)  # async caller invoking sync fn

# === idempotency (requires explicit kind on the decorator) ===
result = await place_order(req, key="order_42")
result = place_order.sync(req, key="order_42")

# === fire-and-forget ===
h = await place_order.submit(req, key="order_42")
job = await h.wait()                   # async wait, returns Job
job = h.wait_sync(timeout=30)          # sync wait

# === read API (always sync) ===
recorded.get(job_id)
recorded.list(kind="broker.*", status="failed",
              where_data={"customer_id": 123},
              limit=50)
recorded.last(10, kind="broker.*")
recorded.connection()                  # escape hatch for raw SQL
```

The bare call returns whatever the wrapped function returns — the decorator is transparent. `Job` is what you get from `JobHandle.wait()` or via the read API; you never silently lose the function's natural return type.

### Decorator parameters

| param | purpose | default |
|---|---|---|
| `kind` | string identifier; **required when `key=` is used** | `f"{fn.__module__}.{fn.__qualname__}"` |
| `request` | typed model validating input | none — store raw |
| `response` | typed model for response (lossy, opt-in) | none — store raw full payload |
| `data` | typed projection over the response, queryable | none |
| `error` | typed model for errors | none — store raw exception info |

`response` is **always full-fidelity by default**. Typing it is opt-in lossiness. `data` is the place to put the small queryable projection. The audit invariant — "the raw response is always recorded" — holds unless the caller explicitly overrides it.

For FastAPI users: a small optional helper `recorded.fastapi.capture_request(request)` returns a serializable envelope (method, path, query, headers, body) suitable for the request slot. `kind` stays as the function-level identifier; transport metadata belongs in the payload, not the kind name.

### Call-site parameters

The library reserves these names on every call surface (bare, `submit`, `sync`, `async_run`); the wrapper extracts them before invoking the wrapped function. If your function takes a parameter with one of these names, rename it.

| param | purpose | default |
|---|---|---|
| `key` | idempotency key (requires explicit `kind=` on the decorator) | None |
| `retry_failed` | when joining via `key`, whether to retry on prior failure | True |

## Job lifecycle

Every call — bare or submitted — produces a row that transitions through the same states:

```
              INSERT                UPDATE                UPDATE
nothing  ────────────►  pending  ────────────►  running  ──────────►  completed | failed
```

Three writes per call. The lifecycle is identical regardless of who orchestrates it — only the orchestrator differs:

- **Bare call**: the caller's wrapper drives all three transitions inline.
- **Submitted call**: the caller's wrapper does only the INSERT (pending) and returns a handle; the worker does the running transition (atomic claim) and the terminal UPDATE.

Same observability, same reaper behavior, same idempotency semantics, same `attach()` semantics. The only operational difference is whether execution happens on the caller's coroutine/thread or on the worker loop.

This costs the bare call ~1ms (one extra UPDATE vs. an "insert directly as running" optimization). At broker-API scale that's <1%, and the consistency in mental model is worth it.

## Idempotency

When you supply `key=` at the call site, the library enforces "at most one active execution per `(kind, key)` pair":

```python
@recorder(kind="broker.place_order", data=OrderView)  # explicit kind required
async def place_order(req): ...

# first call
result = await place_order(req, key="order_42")
# second call — sees the existing record, joins or returns its result
result = await place_order(req, key="order_42")
```

Behavior on collision:
- Existing row is `completed` → return its response without re-executing.
- Existing row is `pending` or `running` → wait for it to reach terminal, then return its result.
- Existing row is `failed` → retry by default (insert a new row); pass `retry_failed=False` to return the most-recent failed Job without retrying.

Multiple `failed` rows per `(kind, key)` are allowed and preserved as history. At most one row in `pending`/`running`/`completed` exists at a time per key, enforced by a partial unique index.

### Why `key=` requires explicit `kind=`

The default `kind` is `f"{fn.__module__}.{fn.__qualname__}"`. If a function is renamed or moved, the auto-derived kind silently changes — and the idempotency keyspace silently changes with it. A retry after a refactor would re-execute work that should have been deduplicated. This is exactly the failure mode idempotency was supposed to prevent.

To prevent the silent break, the library **raises at call time** the first time `key=` is passed against a function whose decorator has an auto-derived kind. (Decoration time can't know whether the caller will use `key=`; refusing every auto-kind decoration would defeat the bare-decorator convenience.) The result is the same — the user sees a loud error before any silent re-execution:

```python
@recorder  # auto-kind
async def place_order(req): ...

await place_order(req, key="order_42")
# RuntimeError: place_order uses an auto-derived kind ("myapp.broker.place_order").
# Idempotency keys require an explicit kind to remain stable across renames.
# Add: @recorder(kind="broker.place_order")
```

The library refuses to compile this rather than warning. Forces a one-time deliberate choice: when you opt into idempotency, you also opt into naming the *logical action* explicitly — independent of what your function happens to be called.

`key` is namespaced by `kind`. Renaming the function with the same explicit kind is safe; changing the kind value breaks idempotency continuity (which is correct — a different action has a different identity).

## Mid-execution attach

```python
from recorded import attach

@recorder(kind="broker.place_order", data=OrderView)
async def place_order(req):
    response = await broker.place_order(req)
    attach("correlation_id", response.broker_correlation_id)
    attach("rate_limit_pct", await broker.rate_limit_status())
    return response
```

`attach(key, value)` accumulates a key/value pair into a buffer for the currently-executing recorded function. The buffer is flushed to `data_json` once at completion. This avoids per-attach SQLite writes and the contention they'd cause under loops.

If you need crash-safe intermediate durability (rare), `attach(key, value, flush=True)` writes through immediately via `json_patch`.

Implemented via `contextvars`, which propagate naturally through `await` boundaries and across `asyncio.gather` (each task gets its own context). If `attach()` is called outside a recorded function, it raises. Tasks/threads spawned by the wrapped function won't see the current job unless they explicitly propagate the context — document the limitation.

Conflict rule: if both `data=Projection` and `attach()` are used, the projection populates initial keys; attaches merge in last-write-wins. Caller owns avoiding key collisions.

## Exception hierarchy

All library-raised exceptions inherit from `RecordedError`. Two main subtrees plus standalones grouped where the grouping is real:

```
RecordedError
├── UsageError                       # programming/configuration mistakes
│   ├── ConfigurationError           # bad decorator setup (key+auto-kind, bad model type)
│   ├── AttachOutsideJobError        # attach() called outside a recorded function
│   ├── SyncInLoopError              # .sync() called from inside a running event loop
│   ├── RecorderClosedError          # operation on a shut-down Recorder
│   └── SerializationError           # value doesn't fit the registered slot model
└── IdempotencyError                 # idempotency-keyed call outcomes
    ├── JoinedSiblingFailedError     # joined sibling terminated as failed
    ├── JoinTimeoutError             # also inherits stdlib TimeoutError
    └── IdempotencyRaceError         # rare post-INSERT lookup-race failure
```

Rules:

- **The wrapped function's own exceptions propagate verbatim.** The library records the failure in `error_json` and re-raises the original. We do not wrap user-domain errors in our hierarchy — if the broker raised `BrokerError`, the caller catches `BrokerError`.
- **Catch at any level of specificity.** `except RecordedError:` catches every library-raised error; `except UsageError:` catches "you used the API wrong"; `except IdempotencyError:` catches all idempotency outcomes; specific classes catch one situation.
- **`JoinTimeoutError` multi-inherits `TimeoutError`** so `except TimeoutError:` callers still catch it.
- **Concrete classes carry structured fields** (e.g. `JoinedSiblingFailedError.sibling_job_id`, `SerializationError.value`) so callers can act programmatically, not just log a string.
- **`NotImplementedError` (stdlib) is used for the phase-2 `.submit` stub** — semantically the right shape, replaced when the worker lands.

## Schema

```sql
CREATE TABLE jobs (
  id              TEXT PRIMARY KEY,    -- uuid4().hex
  kind            TEXT NOT NULL,
  key             TEXT,                -- idempotency key, NULL when not used
  status          TEXT NOT NULL,       -- pending | running | completed | failed
  submitted_at    TEXT NOT NULL,       -- ISO8601 UTC
  started_at      TEXT,
  completed_at    TEXT,
  request_json    TEXT,
  response_json   TEXT,                -- always full unless caller opts into lossy
  data_json       TEXT,                -- typed projection + attached values
  error_json      TEXT
);

CREATE INDEX idx_jobs_status_kind     ON jobs(status, kind);
CREATE INDEX idx_jobs_kind_submitted  ON jobs(kind, submitted_at);

-- enforces "at most one active row per (kind, key)"
CREATE UNIQUE INDEX idx_jobs_key_active
  ON jobs(kind, key)
  WHERE key IS NOT NULL AND status IN ('pending', 'running', 'completed');
```

WAL mode required. **SQLite 3.35.0+ required** (for `UPDATE ... RETURNING`, used by atomic claim). Python's built-in `sqlite3` on most modern platforms qualifies; RHEL 8 and old Debian builds may need a SQLite update.

Power users can add generated columns / additional indexes over `data_json` as their query patterns demand.

ID is `uuid.uuid4().hex` — unique, no monotonic guarantees, no cross-process coordination required. Sort order comes from `submitted_at`. Apps that want time-prefixed IDs override via `id_generator=` in `recorded.configure(...)`.

## Read API

Minimal primitives. The library knows the schema-per-`kind` from the decorator registry, so reads return rehydrated typed values where models were registered, raw dicts where not.

```python
recorded.get(job_id) -> Job | None
recorded.list(
    kind: str | None = None,         # supports glob: "broker.*"
    status: str | None = None,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    where_data: dict[str, Any] | None = None,  # equality on top-level keys only
    limit: int = 100,
    order: str = "desc",
) -> Iterator[Job]
recorded.last(n: int = 10, kind: str | None = None, status: str | None = None) -> list[Job]
recorded.connection() -> sqlite3.Connection
```

`where_data={"customer_id": 123}` compiles to `WHERE json_extract(data_json, '$.customer_id') = ?`. **Equality only, top-level keys only.** Anything richer — `>`, `like`, `in`, joins, aggregations — goes through `recorded.connection()` and raw SQL. The line we hold: this is a read API, not a query language.

Implementation note: use `json_extract(...)` rather than the `->>` operator. `json_extract` is universal since SQLite 3.9.0; `->>` requires 3.38.0+ and would push our minimum version up by a year for no functional gain.

`Job` shape:

```python
@dataclass
class Job:
    id: str
    kind: str
    key: str | None
    status: str
    submitted_at: str
    started_at: str | None
    completed_at: str | None
    request: Any         # rehydrated if a model was registered, else dict
    response: Any
    data: Any | None
    error: Any | None

    @property
    def duration_ms(self) -> int | None:
        # computed from started_at/completed_at; None if not terminal

    def to_prompt(self) -> str:
        # Markdown blob: kind, request, response, error, attach trail.
        # Designed for copy-paste into Claude/ChatGPT to debug failures.
```

The same surface is available on an explicit `Recorder` instance: `r.get()`, `r.list()`, `r.last()`, `r.connection()`.

### Agent introspection (note, not a feature)

The minimal read API plus `Job.to_prompt()` is what makes "agent introspects its own action history" tractable from outside the library. An MCP server exposing `recorded.last()` and `recorded.list()` is a thin wrapper anyone can build in their own package; the core data model and rehydration are what enable it. We don't ship one in core — we make it cheap to ship one elsewhere.

## Worker

The worker is **lazy-started on first `.submit()`**. Bare calls and cross-mode shims (`.sync()`, `.async_run()`) execute inline in the caller's context and never spin up a worker. A script that only ever uses bare calls runs without any background thread.

When started, the worker is **a single asyncio event loop in its own thread**. Concurrency is task spawning, not pool sizing.

```python
async def _worker_main():
    while not shutdown:
        job = await asyncio.to_thread(claim_one)   # atomic claim via UPDATE..RETURNING
        if job is None:
            await asyncio.sleep(poll_interval)
            continue
        asyncio.create_task(_execute(job))         # many in flight on one loop
```

Per-job execution:

```python
async def _execute(job):
    fn = registry.lookup(job.kind)
    if iscoroutinefunction(fn):
        result = await fn(job.request)
    else:
        result = await asyncio.to_thread(fn, job.request)
    await asyncio.to_thread(write_completion, job, result, attached)
```

SQLite I/O via `asyncio.to_thread` keeps the loop spinning during disk writes; the implicit threadpool used by `to_thread` is stdlib-managed. **No `aiosqlite` dependency** — the stdlib pattern handles this correctly.

### Cross-mode shims (no worker required)

- **`.sync(req)`** (sync caller, async-decorated function): uses `asyncio.run()` per call. ~10–50ms loop-setup overhead per call. Intended for **one-off entry points** (CLI main, REPL exploration, simple scripts); not for tight loops. For many calls from a sync context, decorate as async and call from an async context, or use a single `asyncio.run(main())` that awaits all calls. If `.sync()` is called from inside an already-running loop, raises with a message pointing at `await place_order(req)`.
- **`.async_run(req)`** (async caller, sync-decorated function): `await asyncio.to_thread(fn, req)`. Cheap. Contextvars propagate through `to_thread` automatically, so `attach()` works.

### Atomic claim

```sql
UPDATE jobs
   SET status='running', started_at=?
 WHERE id IN (SELECT id FROM jobs
              WHERE status='pending'
              ORDER BY submitted_at
              LIMIT 1)
   AND status='pending'
RETURNING *;
```

Multi-process safe; SQLite WAL handles concurrent writers.

### Stuck-row reaper

On `Recorder.start()`:

```sql
UPDATE jobs
   SET status='failed',
       completed_at=?,
       error_json=json_object('type','orphaned','reason','process_died_while_running')
 WHERE status='running'
   AND started_at < ?  -- now - threshold
```

Default threshold: 5 minutes, configurable. Eliminates the entire "why is my job hung" class of issues from process death.

If a reaper'd task later attempts to write completion, the conditional `UPDATE ... WHERE status='running'` no longer matches and the late completion is silently dropped — the reaper's mark stands. Long-running tasks should bump the reaper threshold (per-kind override is design-for-future, not v1) or use submit-and-poll where the worker's lifetime is longer than any single job.

### Wait mechanism (unified)

`JobHandle.wait()`, `.wait_sync()`, `.sync()`-joining-an-active-row, and idempotency-key-collision-on-active-row all use the same primitive: "subscribe to terminal status for this `job_id`." In-process subscribers register a future on the worker; the writer of the terminal status resolves all matching futures. Cross-process fallback: poll. One thing to build, used everywhere.

### BYO worker (escape hatch)

Library exposes a documented contract:

```python
class WorkerContract:
    def claim_one(self, kinds: list[str] | None = None) -> Job | None: ...
    def complete(self, job_id: str, response, data=None) -> None: ...
    def fail(self, job_id: str, error: Exception | dict) -> None: ...
```

Anyone — Celery worker, k8s cron, custom script — can drive jobs by implementing against this. The default async loop is just one implementation.

## Multi-process notes

- **FastAPI under `uvicorn --workers N`**: each process lazy-starts its own worker on first `.submit()`. All N compete for `pending` rows via atomic claim. Correct, gives parallelism for free.
- **Reaper races across processes**: the reaper query is itself a conditional UPDATE — at-most-once per row regardless of who runs it.
- **Idempotency across processes**: enforced by the partial unique index on the DB. INSERT-then-catch-violation pattern coordinates atomically.
- **Cross-process wake**: not provided in v1. Polling at 200ms is the cross-process mechanism. Sub-millisecond cross-process push is a workload we're not targeting yet.
- **Heavy write contention** under high N×M concurrency is a known unmeasured concern; we'll stress-test before claiming the threadpool model is sufficient at scale.
- Single SQLite file is fine to ~thousands of jobs/sec for I/O-bound workloads. Past that, this isn't the right tool.

## Recorder lifecycle

The decorator is **decoupled** from the Recorder lifecycle. It registers function metadata (kind, schemas) in a module-level registry; dispatch to a Recorder happens when the wrapped function is *called*.

```python
import recorded
from recorded import recorder

# default: lazy module-level singleton at ./jobs.db
@recorder(kind="broker.place_order", data=OrderView)
async def place_order(req): ...

await place_order(req)   # uses the default Recorder

# configure once at app startup if you want non-defaults
recorded.configure(path="/var/lib/myapp/jobs.db")

# explicit instance (tests, FastAPI DI, multi-DB cases)
async with recorded.Recorder(path=":memory:") as r:
    await place_order(req)   # routed to r within the with-block via contextvar
```

`Recorder.shutdown()` is **idempotent** — safe to call multiple times across overlapping lifecycle hooks (atexit, FastAPI lifespan, explicit shutdown in tests).

DB path resolution: explicit `Recorder(path=...)` → `recorded.configure(path=...)` → `./jobs.db` (cwd default).

## Timestamps

`submitted_at`, `started_at`, `completed_at` are stored as **TEXT in ISO8601 UTC**, fixed-width, microsecond precision, `Z` suffix (not `+00:00`). One helper enforces the format:

```python
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
```

Used everywhere a timestamp is written. Lexicographic sort = chronological sort, so `ORDER BY submitted_at` and `WHERE submitted_at > '2026-04-26T...'` work without conversion. Browsable in the `sqlite3` REPL without unixepoch wrapping.

## CLI

`python -m recorded tail` — live-tails new rows to stdout in JSON-line format, polling the DB. Supports `--kind=broker.*`, `--status=failed`, `--limit=N`, `--follow`. Stdlib only (no `rich` or other UI deps); colors and TUI are user-pipe territory (`jq`, `bat`, etc.).

Other subcommands: `python -m recorded last [N]`, `python -m recorded get <job_id>`, `python -m recorded get <job_id> --prompt` (prints `Job.to_prompt()` for paste into an LLM).

## Deferred (not in v1)

- **Replay** — `recorded.replay(job_id)`: re-validate stored request against the current schema, re-invoke. Mostly mechanical; ship after the core stabilizes.
- **Schema versioning and drift detection** — explicitly *not* in v1. We considered a `schema_version` column + decorator parameter but pulled them: shipping the column without the behavior it serves is a Chekhov's gun. Add as one cohesive feature later.
- **Actor / multi-tenant.** v1 has no notion of actor or tenant. Single-user / personal-tool framing. Apps that need this can attach an `actor` key via `attach()` and query through `data_json`, but the library does not provide row-level isolation, query-side filtering by actor, or any multi-tenant guarantees.
- **Cross-process push notification.** Polling at 200ms is the v1 wake mechanism. Considered baking in [honker](https://github.com/russellromney/honker) but it's a native loadable extension with install-time fragility, and the latency gain only matters under workloads we're not targeting yet.
- **Dry-run mode.** Considered as a vibe-coder safety feature. User-implementable in their own code (`if config.dry_run: return mock`); the library's only value-add would be conventionalization. Add when users actually ask.
- **Aggregation helpers** (`total_cost`, `count_by_kind`, etc.). One-liners via `recorded.connection()` and raw SQL. Adding them in the library drifts toward the query-DSL line we explicitly hold.
- **Per-kind reaper threshold.** v1 has a global threshold; per-kind overrides are designed-for but not implemented.
- **`async def`-aware MCP server.** The read API and `to_prompt()` make this a thin wrapper; we make it cheap to ship elsewhere rather than bundling it.

## Why this shape

- **One table, JSON slots:** small enough to read the source in an afternoon. Migrations stay the user's problem if they outgrow it.
- **Async-native with sync first-class:** the audience is FastAPI + async HTTP clients. Async is the natural form; sync gets a one-method-call shim. A sync version of an async library is a thin wrapper; an async version of a sync library is a real architectural change. We do the change once, correctly.
- **Decoration is calling-convention-preserving:** `@recorder` over `async def` produces `async def`; over `def` produces `def`. Bare call returns the natural value. The decorator is transparent — drop-in over existing code.
- **Unified lifecycle (always pending → running → terminal):** simplicity and consistency over a one-write optimization. Same observability for bare and submitted; same idempotency, same reaper, same `attach()`. The wait mechanism becomes one piece of code, used everywhere.
- **`response` always full by default:** preserves "the audit log is faithful" as an invariant. The whole reason you wrap things is to have ground truth later; lossy-by-default would defeat the purpose.
- **Lazy worker:** zero background threads for scripts that don't use `.submit()`. Worker startup is a real signal that you want queue semantics, not an unconditional cost.
- **Reaper on by default:** stuck `running` rows is the single most common source of "this library feels broken" reports for systems like this. ~10 lines makes it disappear.
- **Minimal read primitives, not a query language:** the library knows the schemas, so it owns rehydration. Beyond that, `recorded.connection()` is the escape hatch — we are not Datasette.
- **`contextvars` for `attach()`:** propagate through `await` and across `asyncio.gather` task isolation. The "contextvars are confusing" reputation comes from misunderstanding how they interact with task creation, not from the code we write.
- **`uuid4().hex` for IDs:** unique, zero state, no cross-process coordination. Sort order comes from `submitted_at`. ULIDs were a vibe choice that turned out to require monotonic-within-millisecond state and locking — not worth the complexity.
- **Idempotency requires explicit `kind=`:** the auto-derived kind silently changes when functions are renamed. Combining auto-kind with `key=` gives a false sense of safety that fails exactly when you needed it (after a refactor, on retry). Refusing to compile is louder than warning, and the cost is one explicit name once per idempotent action.
- **No actor, no schema_version, no honker, no dry-run, no aggregation helpers:** every feature that didn't earn its keep got cut. The library got smaller through design, not bigger.
