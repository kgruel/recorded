# Reading what's been recorded

Three surfaces for inspecting the log: the Python read API, the CLI, and the
raw SQLite file. They all read from the same `jobs` table — pick whichever
is most ergonomic for your task.

> Architecture context: [`docs/OVERVIEW.md` — the read side](../OVERVIEW.md#the-read-side)

## The `Job` dataclass

Every read returns a `Job` (or an iterator/list of them):

```python
@dataclass
class Job:
    id: str                      # uuid4().hex
    kind: str                    # "broker.fetch_quote"
    key: str | None              # idempotency key, if any
    status: str                  # "pending" | "running" | "completed" | "failed"
    submitted_at: str            # ISO8601 UTC, microsecond, Z-suffix
    started_at: str | None
    completed_at: str | None
    request: Any                 # rehydrated to the request= model if registered
    response: Any                # rehydrated to the response= model if registered
    data: Any | None             # data slot
    error: Any | None            # error slot
```

Plus two methods:

- `.duration_ms` — wall time from `started_at` to `completed_at`, in
  milliseconds. `None` for non-terminal rows.
- `.to_prompt()` — render the job as Markdown for paste into an LLM. See
  [queries](queries.md#feeding-history-back-to-an-llm).

Slot rehydration uses the adapter registered at `@recorder` decoration. If the
decorator declared `response=OrderReply`, then `job.response` is an
`OrderReply` instance. If no model was registered, `job.response` is a plain
dict.

## `recorded.last(n=10, *, kind=None, status=None)`

Most recent N jobs in submission order, newest first.

```python
import recorded

# Last 10 jobs, any kind
for job in recorded.last(10):
    print(job.id, job.kind, job.status)

# Filtered: glob on kind, exact match on status
recorded.last(20, kind="broker.*", status="failed")
```

Both filters are optional. `kind` accepts a SQLite `GLOB` pattern (`*` and
`?` wildcards). `status` is one of `"pending"`, `"running"`, `"completed"`,
`"failed"` — exact match.

## `recorded.get(job_id)`

Single row by id; returns `Job | None`.

```python
job = recorded.get("3a8f...")
if job and job.status == "completed":
    print(job.response)
```

## `recorded.list(...)`

For richer queries — equality predicates on `data` keys, time windows, exact
key match. See [queries](queries.md) for the full surface.

```python
for job in recorded.list(
    kind="orders.*",
    status="completed",
    where_data={"customer_id": 7},
    since="2026-04-01T00:00:00Z",
    limit=100,
):
    ...
```

## `recorded.connection()`

Escape hatch — a raw `sqlite3.Connection` for arbitrary SQL. See
[queries — raw SQL](queries.md#raw-sql-via-connection).

## The CLI

```
python -m recorded last [N] [--kind GLOB] [--status S] [--path P]
python -m recorded get  <job_id> [--prompt] [--path P]
python -m recorded tail [--kind GLOB] [--interval S] [--path P]
```

Stdlib only. Each invocation builds its own short-lived `Recorder(path=...)`
and shuts it down — never touches the module-level singleton, so the CLI
won't spawn a worker thread.

`--path` defaults to `./jobs.db`. Override per invocation when your
application uses a non-default location.

### `last`

One line per job, ordered newest-first.

```bash
$ python -m recorded last 5
2026-04-27T15:02:14.819Z  completed  orders.place                   3a8f1c2d  key=order-42
2026-04-27T15:02:13.001Z  failed     broker.fetch_quote             7c3d9b1e  key=-
2026-04-27T15:02:11.456Z  completed  orders.place                   1d4e6f8a  key=order-41
...
```

Each row: `submitted_at  status  kind  short-id  key=...`. The `short-id` is
the first 8 chars of `job.id` — enough to disambiguate, paste back into
`get <job_id>` to look up the full row.

### `get`

```bash
$ python -m recorded get 3a8f1c2d
```

Default output: pretty-printed JSON of the full `Job` dataclass — every slot,
every timestamp.

`--prompt` switches to Markdown via `Job.to_prompt()`, ready to paste into an
LLM:

```bash
$ python -m recorded get 3a8f1c2d --prompt
# orders.place — completed

- id: 3a8f1c2d...
- key: order-42
- submitted: 2026-04-27T15:02:14.819Z
- ...

## Request
```json
{ "symbol": "AAPL", "qty": 100 }
```

## Response
```json
{ "order_id": "ord-9281", "filled_at": "..." }
```
```

### `tail`

Stream new terminal rows as they land. Polls `recorded.list()` with a moving
watermark; new completed/failed rows print as one-liners.

```bash
$ python -m recorded tail --kind 'broker.*' --interval 0.5
```

`--interval` is the poll cadence in seconds (default 1.0). Ctrl-C to exit.
Historical rows are not replayed — `tail` starts from "now"; use
`last` for history.

## Reading the raw SQLite file

`jobs.db` is a normal SQLite file — any tool that reads SQLite reads it.

```bash
sqlite3 jobs.db "
  SELECT kind, status,
         (julianday(completed_at) - julianday(started_at)) * 86400000 AS duration_ms
  FROM jobs
  WHERE submitted_at > '2026-04-27T00:00:00Z'
  ORDER BY submitted_at DESC LIMIT 20;
"
```

Schema:

```sql
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  key           TEXT,
  status        TEXT NOT NULL,
  submitted_at  TEXT NOT NULL,
  started_at    TEXT,
  completed_at  TEXT,
  request_json  TEXT,
  response_json TEXT,
  data_json     TEXT,
  error_json    TEXT
);
```

`duration_ms` is a Python property on the `Job` dataclass, **not** a stored
column — compute it from `started_at` / `completed_at` if you want it in SQL.
