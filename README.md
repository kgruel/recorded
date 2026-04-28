# recorded

`@recorder` turns every call to a Python function into a row in SQLite —
args, return value, exceptions, duration. Inspect later with the read API,
the CLI, or any tool that talks to a SQLite file.

> **Privacy:** `recorded` writes function arguments, return values, and
> exceptions to SQLite verbatim. The DB at `jobs.db` becomes a durable
> record of everything wrapped — including any secrets the function
> handled. Use [`recorded.fastapi.capture_request(redact_headers=...)`](docs/usage/fastapi.md)
> for HTTP request capture; for general functions, redact sensitive
> arguments yourself before they cross the decorator boundary. See
> [configuration](docs/usage/configuration.md) for redaction options.

```python
from recorded import recorder, last

@recorder(kind="orders.place")
def place_order(symbol: str, qty: int) -> dict:
    return broker.place_order(symbol=symbol, qty=qty)

place_order("AAPL", 100)            # runs as normal — but recorded
print(last(1)[0].response)          # {'order_id': 'ord-9281', ...}
```

Need leader-process durability or fire-and-wait handles? See
[Advanced: durable submission](#advanced-durable-submission-via-leader-process).

The library is small by design. A four-slot row, a three-write lifecycle,
and a transparent decorator — that's the spine. Idempotency keys, typed
projections, queryable data slots, FastAPI integration, and cross-process
`.submit()` all layer on without changing the bare-call shape: removing
`@recorder` doesn't change return-type or exception-type shape, only the
side effect goes away.

---

## What it gives you

- **Audit trail without ceremony.** Every call is a row, automatically.
  The bare call returns the wrapped function's natural value — adding or
  removing `@recorder` is a safe edit at every call site.
- **Idempotency keys.** Pass `key="..."` and the second call is free.
  Same key in another process? It joins the in-flight work and returns
  the same response.
- **Typed slots.** Pydantic v2 or `@dataclass`, duck-typed (never
  imported). Validates on the way in, rehydrates typed objects on the
  way out.
- **Queryable projections.** Declare `data=Model` and
  `recorded.query(where_data=...)` filters by JSON-extracted columns.
  Anything richer drops to `recorded.connection()` and raw SQL.
- **CLI + LLM-ready prompts.** `python -m recorded last/get/tail`, plus
  `Job.to_prompt()` for pasting a row back into a model as context.
- **Optional cross-process submission.** `.submit()` enqueues a job that
  a separate leader process executes; `JobHandle.wait()` blocks for
  completion. Opt-in tier — bare-call paths are leader-free. See
  [Advanced: durable submission](#advanced-durable-submission-via-leader-process).

## What it isn't

- **A multi-process job queue.** SQLite WAL handles concurrent writers
  fine, but cross-process `JobHandle.wait()` falls back to polling at
  200ms cadence — don't expect Redis-grade fan-out latency.
- **A query DSL.** `where_data=` is equality on top-level keys, by
  design. Anything richer goes through `recorded.connection()` and raw
  SQL — the library doesn't grow a query language.
- **A schema-versioning library.** One table, one schema, frozen.
- **A distributed-tracing replacement.** Use OpenTelemetry for spans
  across services. Use `recorded` when you want a row per call you can
  grep tomorrow.
- **A heavy dep.** Stdlib only for the core. Pydantic / FastAPI /
  Starlette are auto-detected if installed but never imported.

---

## Idempotency — the second call is free

```python
@recorder(kind="orders.place")
def place_order(req): ...

place_order(req, key="order-42")    # runs the function, records the row
place_order(req, key="order-42")    # returns the cached response — no broker call
```

Works across processes via a partial unique index on `(kind, key)`.
Failed jobs retry by default; `retry_failed=False` raises
`JoinedSiblingFailedError` carrying the prior error payload instead.

> Deeper: [usage/idempotency.md](docs/usage/idempotency.md).

## Queryable projections — `data=Model` + `recorded.query(...)`

```python
class OrderView(BaseModel):
    order_id: str
    customer_id: int

@recorder(kind="orders.place", data=OrderView)
def place_order(req): ...

# Auto-projects from the response (or use attach() for fields not on the response).
recorded.query(kind="orders.*", where_data={"customer_id": 7})
```

The `data` slot is the queryable index — declare what you'll filter by;
everything else stays in the wide `response` slot. The full response is
recorded faithfully (the audit invariant); `data` is just the slice
you'll grep on.

> Deeper: [usage/typed-slots.md](docs/usage/typed-slots.md).

## Advanced: durable submission via leader process

Most users won't need this. The bare `@recorder` path above runs in the
caller's process, records the row, and returns — no extra infrastructure.
`.submit()` is the opt-in tier for "fire here, run there, wait or come
back later for the result":

```python
handle = build_report.submit(spec, key="daily-2026-04-27")

job = handle.wait_sync(timeout=60.0)        # from sync code
job = await handle.wait(timeout=60.0)       # from async code
```

`.submit()` enqueues a `pending` row and returns a handle; a separate
**leader process** (`python -m recorded run --import myapp.tasks`) claims
and executes the row. Without a leader running, `.submit()` raises
`ConfigurationError` rather than silently degrading; misconfigured
deployments fail loudly on the first submit. The reaper sweeps orphaned
`running` rows on the next leader-process bootstrap, so a crashed leader
doesn't leave idempotency keys held forever.
`recorder.is_leader_running()` is the deployment health check.

> Deeper: [usage/workers.md](docs/usage/workers.md).

---

## Where to read next

The full feature tour, in roughly this order. Each guide is independent —
skip to the one you need.

| guide | covers |
|---|---|
| [usage/decorator.md](docs/usage/decorator.md) | the decorator, sync vs async, cross-mode shims |
| [usage/configuration.md](docs/usage/configuration.md) | `configure()`, lifecycles, multi-recorder safety |
| [usage/reading.md](docs/usage/reading.md) | `last`, `get`, `query`, `connection`, the CLI |
| [usage/idempotency.md](docs/usage/idempotency.md) | `key=`, `retry_failed`, the partial-unique-index trick |
| [usage/typed-slots.md](docs/usage/typed-slots.md) | the four slots, `attach()`, `attach_error()` |
| [usage/queries.md](docs/usage/queries.md) | `recorded.query(...)`, raw SQL, `Job.to_prompt()` |
| [usage/fastapi.md](docs/usage/fastapi.md) | lifespan integration, `capture_request`, two-shape pattern |
| [usage/workers.md](docs/usage/workers.md) | `.submit()`, the leader process, the reaper |
| [usage/errors.md](docs/usage/errors.md) | exception hierarchy, what to catch where |

Or jump in by need — see the [usage guide index](docs/usage/README.md).

**Architecture:** [docs/HOW.md](docs/HOW.md) is the contributor
tour; [docs/WHY.md](docs/WHY.md) is the authoritative spec.

**Runnable examples:** [docs/examples/](docs/examples/) — LLM-CLI replay,
FastAPI service, batch HTTP consumer.

---

## Install

```bash
pip install recorded
```

- Python 3.10+
- SQLite 3.35.0+ (for `UPDATE ... RETURNING`). The stdlib `sqlite3` on
  most modern platforms qualifies; RHEL 8 / older Debian may need a
  SQLite update.
- Pydantic is **optional**. Auto-detected at import time if installed.

## Local development

```bash
uv venv
uv pip install -e ".[dev]"
uv run pytest
```

`pip install -e ".[dev]"` works equivalently if `uv` is unavailable. The
test suite uses real SQLite (no stdlib mocks) and runs in ~5 seconds.
