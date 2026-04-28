# Configuration

Two patterns for getting a `Recorder`: the module-level singleton (via
`configure()`), and explicit `Recorder(...)` instances. Most applications
want the singleton; the explicit form covers tests, FastAPI dependency
injection, and multi-database scenarios.

> Architecture context: [`docs/HOW.md` — lifecycle integration](../HOW.md#lifecycle-integration)

## `recorded.configure(...)`

Call once, at application startup, before any decorated function fires.

```python
import recorded

recorded.configure(path="/var/lib/myapp/jobs.db")
```

Signature:

```python
def configure(
    path: str | None = None,
    *,
    reaper_threshold_s: float | None = None,
    worker_poll_interval_s: float | None = None,
    join_timeout_s: float | None = None,
    warn_on_data_drift: bool | None = None,
) -> Recorder
```

| parameter | default | purpose |
|---|---|---|
| `path` | `"./jobs.db"` | SQLite file path. Created on first write. |
| `reaper_threshold_s` | `300.0` | how old a `running` row must be before the reaper marks it failed. See [workers — the reaper](workers.md#the-reaper). |
| `worker_poll_interval_s` | `0.2` | how often the leader polls for `pending` rows. See [workers](workers.md). |
| `join_timeout_s` | `30.0` | default timeout for idempotency-collision joins and `JobHandle.wait()`. See [idempotency](idempotency.md) and [workers](workers.md). |
| `warn_on_data_drift` | `True` | log a deduped warning when a declared `data` slot ends up empty (response shape didn't project). See [typed slots](typed-slots.md#the-data-slot--queryable-projections). |

`Recorder.__init__` also accepts `leader_heartbeat_s` (default 5 s) and
`leader_stale_s` (default 30 s); not currently forwarded through
`configure()`. See [workers — leader heartbeat](workers.md#leader-heartbeat).

`None` values fall through to the `Recorder.__init__` defaults — pass only
the parameters you want to override.

### Configure-once

`configure()` is **configure-once**. The first call constructs the default
`Recorder` and registers `atexit.register(recorder.shutdown)`. Subsequent
calls return the existing default unchanged — no warning, no exception, no
re-construction:

```python
recorded.configure(path="./prod.db")     # constructs and installs
recorded.configure(path="./other.db")    # no-op; returns the prod recorder
```

This is intentional: in libraries that import each other, no caller can
silently swap the default out from under another. Tests bypass the
configure-once guard by constructing a `Recorder(...)` directly.

### Async context manager pattern

`Recorder` is also an async context manager, which is useful inside framework
lifespans:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
import recorded

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with recorded.configure(path="/var/lib/app/jobs.db") as r:
        yield
    # connection closed on exit

app = FastAPI(lifespan=lifespan)
```

On `__aenter__`: connection bootstrapped (which also runs the reaper sweep).
On `__aexit__`: connection closed.

Note that `.submit()` calls inside FastAPI handlers require a *separate*
leader process (`python -m recorded run`) running against the same path —
the FastAPI app process itself doesn't execute submitted jobs. See
[workers](workers.md).

`configure()` returns the same `Recorder` whether called directly or as
`async with` — the difference is whether you bind the connection lifetime to
your `with` block or to interpreter exit.

## Explicit `Recorder(path=...)`

For tests, FastAPI dependency injection, or when one process talks to
multiple SQLite files:

```python
from recorded import Recorder

with Recorder(path="/tmp/scratch.db") as r:
    # r.last(), r.get(...), r.query(...), r.connection() all available
    ...
```

Important asymmetry: **a directly-constructed `Recorder` does NOT register
`atexit`**. Only the *configured* default does. If you build a `Recorder`
outside a `with` block, you're responsible for calling `r.shutdown()`
explicitly so the SQLite connection closes cleanly (WAL files get
checkpointed; otherwise stale `-wal`/`-shm` files may linger).

```python
r = Recorder(path="./scratch.db")
try:
    ...
finally:
    r.shutdown()
```

## `Recorder.shutdown()`

Idempotent. Closes the SQLite connection. Safe to call multiple times —
also safe to call alongside the `atexit` hook from `configure()`.

After `shutdown()`, the recorder rejects new operations with
`RecorderClosedError`. There is no in-process worker to drain — `.submit()`
work runs in a separate leader process and is unaffected by the submitting
process's `Recorder.shutdown()`.

## Pre-built recorder

`recorded.configure()` doesn't currently accept a pre-built `Recorder`
instance — only construction parameters. If you need to install your own
instance as the module default (e.g. a test fixture, or a custom subclass),
the private hook `recorded._recorder._set_default_for_testing(r)` covers
test fixtures; please open an issue if you want a public production hook.

## Multi-process and multi-recorder safety

SQLite WAL handles concurrent writers across processes — multiple processes
can write to the same `jobs.db` safely. The atomic `_claim_one` query
(`UPDATE ... RETURNING`) is race-safe across leader processes.

The reaper is also multi-process safe: its `UPDATE ... WHERE status='running'
AND started_at < ?` is conditional, so multiple recorders booting against
the same DB don't double-reap a row.

The wait primitive's notify side resolves only same-process subscribers
(bare-call idempotency-collision joins). `JobHandle.wait()` against a row
the leader executes in another process falls back to a polling loop with
`NOTIFY_POLL_INTERVAL_S` (200 ms) cadence — correct, just slower than the
in-process notify path.
