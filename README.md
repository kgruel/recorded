# recorded

**A drop-in audit log for any Python function. Decorate it, call it, and every
invocation lands in a SQLite row — request, response, errors, timing.** Inspect
it later with the read API, the CLI, or any SQLite client.

`recorded` came out of wanting a free audit log for AI agents that call APIs,
but it works for any function call you'd like to keep a record of. The whole
design is built around progressive disclosure: start with one decorator and zero
configuration, then opt into idempotency, background execution, typed slots,
HTTP-framework integration, and queryable projections only as you need them.

> Need the user guides? See [`docs/usage/`](docs/usage/). Want the architecture
> tour? See [`docs/OVERVIEW.md`](docs/OVERVIEW.md). Want the authoritative design
> rationale? See [`docs/DESIGN.md`](docs/DESIGN.md). Working examples live in
> [`docs/examples/`](docs/examples/).

---

## Contents

- [Install](#install)
- [Level 1 — Decorate and forget](#level-1--decorate-and-forget)
- [Level 2 — Look at what happened](#level-2--look-at-what-happened)
- [Level 3 — Pick where the database lives](#level-3--pick-where-the-database-lives)
- [Level 4 — Don't run the same call twice (idempotency)](#level-4--dont-run-the-same-call-twice-idempotency)
- [Level 5 — Run it in the background](#level-5--run-it-in-the-background)
- [Level 6 — Typed slots and queryable projections](#level-6--typed-slots-and-queryable-projections)
- [Level 7 — Inside a web framework (FastAPI)](#level-7--inside-a-web-framework-fastapi)
- [Level 8 — Reading the log later](#level-8--reading-the-log-later)
- [Errors you may want to catch](#errors-you-may-want-to-catch)
- [Architecture and design](#architecture-and-design)
- [Local development](#local-development)

## Install

```bash
pip install recorded
```

Requirements:

- Python 3.10+
- SQLite 3.35.0+ (for `UPDATE ... RETURNING`). The stdlib `sqlite3` on most
  modern platforms qualifies; RHEL 8 / older Debian may need a SQLite update.
- Pydantic is **optional**. If installed, it's auto-detected at import time and
  used for typed slots — never a hard dependency.

---

## Level 1 — Decorate and forget

The minimum-viable use of `recorded`. No setup, no configuration, no read calls.
Decorate a function, call it normally, and every invocation is captured to
`./jobs.db` in your working directory.

```python
from recorded import recorder

@recorder(kind="math.add")
def add(a: int, b: int) -> int:
    return a + b

add(2, 3)  # returns 5; recorded as a row in ./jobs.db
```

That's it. The bare call returns the wrapped function's natural value — adding
or removing `@recorder` does not change return-type or exception-type shape.
A `./jobs.db` file is created on the first call.

> **Read more:** [the `@recorder` decorator](docs/usage/decorator.md)
> · [the wrap-transparency principle](docs/DESIGN.md#2-wrap-transparency-scoped-to-basic)

---

## Level 2 — Look at what happened

Once anything is recorded, you have three ways to inspect it.

**The Python read API**:

```python
import recorded

# Most recent N jobs (optionally filtered by kind glob and status)
for job in recorded.last(5):
    print(f"{job.id} {job.kind} -> {job.status} ({job.duration_ms} ms)")
    print(f"  request:  {job.request}")
    print(f"  response: {job.response}")

# Single job by id
job = recorded.get("abc123...")
```

**The CLI**, for poking around without writing Python:

```bash
python -m recorded last 10                  # last 10 jobs
python -m recorded last 10 --kind 'math.*'  # glob on kind
python -m recorded get <job_id>             # one job
python -m recorded get <job_id> --prompt    # Markdown for pasting into an LLM
python -m recorded tail                     # follow new rows as they land
```

**Or any SQLite client** — it's just a file.

```bash
sqlite3 jobs.db "SELECT kind, status, started_at, completed_at \
                 FROM jobs ORDER BY submitted_at DESC LIMIT 10;"
```

> **Read more:** [reading the log](docs/usage/reading.md)
> · [architecture: the read side](docs/OVERVIEW.md#the-read-side)

---

## Level 3 — Pick where the database lives

The default `./jobs.db` is fine for scripts and notebooks. For an application,
configure once at startup so every recorder writes to the same place.

```python
import recorded

recorded.configure(path="/var/lib/myapp/jobs.db")
```

`configure()` is **configure-once**: subsequent calls return the existing
default Recorder. It also registers `atexit.register(recorder.shutdown)` so the
worker drains cleanly on interpreter exit.

`Recorder` is also an async context manager, which is useful inside framework
lifespans (see [Level 7](#level-7--inside-a-web-framework-fastapi)):

```python
async with recorded.configure(path="/var/lib/myapp/jobs.db") as r:
    ...  # connection bootstrapped, reaper sweep run on enter; drained on exit
```

> **Read more:** [configuration options and Recorder lifecycle](docs/usage/configuration.md)

---

## Level 4 — Don't run the same call twice (idempotency)

Pass `key=` to make a call idempotent. If a job with the same `(kind, key)` is
pending or running, the second call **joins** the in-flight work and returns
its result. If completed, the cached response is returned without re-running.

```python
@recorder(kind="orders.place")
def place_order(req: OrderRequest) -> OrderReply:
    return broker.place(req)

# First call runs the function and records the result
reply = place_order(req, key="order-42")

# Any subsequent call with the same key returns the cached response —
# even from a different process — without re-charging the broker.
reply = place_order(req, key="order-42")
```

Failed jobs retry by default (`retry_failed=True`). To join a prior failure
instead of retrying, pass `retry_failed=False` — the call raises
`JoinedSiblingFailedError` carrying the recorded error payload.

The wrap-transparency rule: keyed-call surfaces always either return the
response **or raise**. They never return a `Job` object representing failure.
To inspect prior failed rows, use the read API
(`recorded.last(kind=..., key=..., status="failed")`).

> **Read more:** [idempotency in depth](docs/usage/idempotency.md)
> · [architecture: the partial-unique-index trick](docs/OVERVIEW.md#idempotency--the-partial-unique-index-trick)

---

## Level 5 — Run it in the background

Use `.submit(...)` instead of calling directly to enqueue the job for a
background worker. Returns a `JobHandle` immediately; the wrapped function
runs on a dedicated asyncio loop in a worker thread.

```python
@recorder(kind="reports.build")
def build_report(spec: ReportSpec) -> Report:
    ...  # slow

handle = build_report.submit(spec, key="daily-2026-04-27")

# Block until terminal (sync caller)
job = handle.wait_sync(timeout=60.0)
print(job.response)

# Or await it from async code
job = await handle.wait(timeout=60.0)
```

The worker is **lazy**: it doesn't spawn until the first `.submit()` call.
A reaper sweeps orphaned `running` rows on connection bootstrap (default
threshold: 5 minutes), so a crashed process won't leave jobs stuck forever.

> **Read more:** [background execution and the worker](docs/usage/workers.md)
> · [architecture: submitted-call trace](docs/OVERVIEW.md#trace-a-call-submitted-path)

---

## Level 6 — Typed slots and queryable projections

A `recorded` row has four JSON slots: `request`, `response`, `data`, `error`.
Each can be optionally validated by a typed model (Pydantic v2 or
`@dataclass`). The adapter is duck-typed — `recorded` never imports Pydantic.

```python
from pydantic import BaseModel

class OrderRequest(BaseModel):
    symbol: str
    qty: int

class OrderReply(BaseModel):
    order_id: str
    filled_at: str

class OrderView(BaseModel):
    """A queryable projection of the response, denormalized into `data`."""
    order_id: str
    symbol: str

@recorder(
    kind="orders.place",
    request=OrderRequest,
    response=OrderReply,
    data=OrderView,
)
def place_order(req: OrderRequest) -> OrderReply:
    return broker.place(req)
```

Inside the function, you can also annotate the row mid-execution with
`recorded.attach(...)` (key-merged into `data`) or `recorded.attach_error(...)`
(set on the failure path):

```python
from recorded import attach, attach_error

@recorder(kind="orders.place", error=BrokerError)
def place_order(req):
    attach("broker_latency_ms", measure())   # merged into data slot
    try:
        return broker.place(req)
    except BrokerError as e:
        attach_error(e)                       # structured error payload
        raise
```

Both `attach()` and `attach_error()` are **silent no-ops outside an active
context** — removing `@recorder` doesn't require deleting them.

> **Read more:** [typed slots and `attach()`](docs/usage/typed-slots.md)
> · [architecture: the slot primitive](docs/OVERVIEW.md#the-single-primitive-the-slot)

---

## Level 7 — Inside a web framework (FastAPI)

Two pieces wire `recorded` into FastAPI cleanly. `Recorder` is an async context
manager, so it drops into a lifespan. And `recorded.fastapi.capture_request`
serializes a `Request` object into a JSON-safe dict for the `request` slot.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
import recorded
from recorded import recorder
from recorded.fastapi import capture_request

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with recorded.configure(path="/var/lib/app/jobs.db"):
        yield

app = FastAPI(lifespan=lifespan)

@recorder(kind="http.checkout")
async def checkout(payload: dict) -> dict:
    ...

@app.post("/checkout")
async def checkout_endpoint(request: Request):
    payload = await capture_request(request)  # method, path, headers, body
    return await checkout(payload)
```

`capture_request` is duck-typed — it works on any object with the same shape as
a Starlette/FastAPI `Request`. `recorded` never imports `fastapi` or
`starlette`.

> **Read more:** [FastAPI integration patterns](docs/usage/fastapi.md)
> · [working example](docs/examples/02_fastapi_app.py)

---

## Level 8 — Reading the log later

Beyond `last()` and `get()`, two more surfaces cover richer queries.

**`recorded.list(...)`** — a filtered iterator with equality predicates:

```python
for job in recorded.list(
    kind="orders.*",
    status="completed",
    where_data={"customer_id": 7},   # equality on top-level data keys
    since="2026-04-01T00:00:00Z",
    limit=100,
):
    ...
```

**`recorded.connection()`** — raw SQLite for anything `list()` doesn't cover:

```python
conn = recorded.connection()
cur = conn.execute("""
    SELECT kind,
           COUNT(*) AS n,
           AVG((julianday(completed_at) - julianday(started_at)) * 86400000) AS avg_ms
    FROM jobs
    WHERE status = 'completed'
    GROUP BY kind
""")
```

**`Job.to_prompt()`** — render a job as Markdown, ready to paste into an LLM
prompt:

```python
job = recorded.get(job_id)
print(job.to_prompt())
# Or from the CLI: python -m recorded get <job_id> --prompt
```

The `to_prompt()` rendering pairs naturally with the agent use case: hand the
last few turns back to the model as context, including the structured request
and response shapes.

> **Read more:** [queries and LLM consumption](docs/usage/queries.md)
> · [LLM threading example](docs/examples/01_claude_turns.py)

---

## Errors you may want to catch

All exceptions are re-exported from the top-level `recorded` namespace:

```python
import recorded

try:
    reply = place_order(req, key="order-42")
except recorded.JoinedSiblingFailedError as e:
    # A prior call with the same key failed; e.sibling_error has the payload.
    ...
except recorded.JoinTimeoutError:
    # JobHandle.wait(timeout=...) elapsed before the row reached terminal.
    ...
except recorded.UsageError:
    # Configuration / call-shape misuse (e.g. key= with auto-derived kind,
    # request=Model with multi-arg call shape).
    ...
```

The full hierarchy: `RecordedError` → `UsageError` (`ConfigurationError`),
`IdempotencyError` (`JoinedSiblingFailedError`, `IdempotencyRaceError`),
`SerializationError`, `RecorderClosedError`, `JoinTimeoutError`,
`SyncInLoopError`.

> **Read more:** [exception hierarchy and what to catch](docs/usage/errors.md)

---

## Architecture and design

- [`docs/usage/`](docs/usage/) — task-oriented user guides, one per
  feature: [decorator](docs/usage/decorator.md),
  [reading](docs/usage/reading.md),
  [configuration](docs/usage/configuration.md),
  [idempotency](docs/usage/idempotency.md),
  [workers](docs/usage/workers.md),
  [typed slots](docs/usage/typed-slots.md),
  [FastAPI](docs/usage/fastapi.md),
  [queries](docs/usage/queries.md),
  [errors](docs/usage/errors.md).
- [`docs/OVERVIEW.md`](docs/OVERVIEW.md) — guided architecture tour:
  schema, lifecycle, the wait primitive, idempotency mechanics, the reaper,
  and the read side. **Start here for contributors.**
- [`docs/DESIGN.md`](docs/DESIGN.md) — authoritative design spec. The "why"
  behind the slot abstraction, three-write lifecycle, dissolution test, and
  every load-bearing decision.
- [`docs/examples/`](docs/examples/) — runnable scripts:
  - `01_claude_turns.py` — record LLM-CLI turns and feed them back as context.
  - `02_fastapi_app.py` — FastAPI lifespan + endpoint integration.
  - `03_api_consumer.py` — sync HTTP consumer with idempotency.

---

## Local development

We use [`uv`](https://github.com/astral-sh/uv) for environment management.

```bash
uv venv
uv pip install -e ".[dev]"
uv run pytest
```

`pip install -e ".[dev]"` works equivalently if `uv` is unavailable. The test
suite uses real SQLite (no mocks of stdlib internals) and runs in ~5 seconds.
