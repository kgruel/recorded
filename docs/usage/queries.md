# Queries

Three layered surfaces for asking the log questions: `recorded.query(...)`
for filtered iteration, `recorded.connection()` for raw SQL, and
`Job.to_prompt()` for handing rows back to an LLM.

> Architecture context: [`docs/HOW.md` — the read side](../HOW.md#the-read-side)

## `recorded.query(...)`

A filtered iterator with equality predicates and time windows. The query is
deferred until the first `next()` call.

```python
import recorded

for job in recorded.query(
    kind="orders.*",
    status="completed",
    where_data={"customer_id": 7, "region": "us-east"},
    since="2026-04-01T00:00:00Z",
    until="2026-04-30T23:59:59Z",
    limit=100,
    order="desc",
):
    print(job.id, job.kind, job.data)
```

| parameter | type | meaning |
|---|---|---|
| `kind` | `str \| None` | SQLite GLOB pattern (`*`, `?`). `None` = all kinds. |
| `status` | `str \| Sequence[str] \| None` | one of `"pending"` / `"running"` / `"completed"` / `"failed"`, or a tuple/list of them (compiled as `IN (...)`). |
| `key` | `str \| None` | exact match on idempotency key. |
| `since` | `str \| datetime \| None` | filter by `submitted_at >= since`. |
| `until` | `str \| datetime \| None` | filter by `submitted_at <= until`. |
| `where_data` | `dict[str, Any] \| None` | equality on top-level keys of `data_json`. Multiple keys AND together. |
| `limit` | `int` | row limit (default 100). |
| `order` | `"asc" \| "desc"` | sort by `submitted_at` (default `"desc"`). |

The iterator buffers all matched rows into memory on first `next()` rather
than streaming a live cursor — holding a cursor across thread boundaries
races concurrent writes. "Lazy" here means "query deferred", not
"row-streamed". Tune `limit` accordingly.

### `where_data` — equality only

`where_data` compiles to `json_extract(data_json, '$.key') = ?` per pair.
Limitations:

- **Top-level keys only**. Nested paths (`"address.city"`) raise
  `ConfigurationError`. Use `recorded.connection()` for nested paths.
- **Equality only**. No ranges, no `IN`, no `LIKE`, no aggregations. Use
  `connection()` for any of those.
- **Multiple pairs AND together**. There's no OR.

This is the **no-DSL line**. Anything richer than equality on top-level
keys goes through `connection()` raw SQL — the library doesn't grow a query
language. The pairing is intentional: most queries are simple equality;
the few that aren't are better expressed as raw SQL than as a half-built
DSL.

For `where_data` to be useful, declare a `data=Model` on the decorator and
project the keys you'll filter on into it. See
[typed slots](typed-slots.md#the-data-slot--queryable-projections).

## Raw SQL via `connection()`

`recorded.connection()` returns the underlying `sqlite3.Connection`:

```python
conn = recorded.connection()

# Aggregate by kind, average completed duration
cur = conn.execute("""
    SELECT kind,
           COUNT(*) AS n,
           AVG((julianday(completed_at) - julianday(started_at)) * 86400000) AS avg_ms
    FROM jobs
    WHERE status = 'completed'
    GROUP BY kind
    ORDER BY n DESC
""")
for kind, n, avg_ms in cur.fetchall():
    print(f"{kind:30} {n:6}  {avg_ms:.1f} ms")
```

Reach into the JSON columns with `json_extract`:

```python
# All orders for customer 7 in April, with the broker's order_id
conn.execute("""
    SELECT id,
           json_extract(data_json, '$.order_id') AS order_id,
           submitted_at
    FROM jobs
    WHERE kind = 'orders.place'
      AND json_extract(data_json, '$.customer_id') = ?
      AND submitted_at BETWEEN ? AND ?
""", (7, "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z"))
```

The connection is read-write — you *can* run `INSERT` / `UPDATE` against it,
but doing so bypasses the lifecycle invariants and the wait primitive. For
manual surgery only.

Use `json_extract(data_json, '$.key')` syntax (not the `->>` shortcut) for
portability across SQLite versions.

## Schema reference

```sql
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,    -- uuid4().hex
  kind          TEXT NOT NULL,       -- "broker.fetch_quote"
  key           TEXT,                -- idempotency key (nullable)
  status        TEXT NOT NULL,       -- pending|running|completed|failed
  submitted_at  TEXT NOT NULL,       -- ISO8601 UTC microsecond Z
  started_at    TEXT,
  completed_at  TEXT,
  request_json  TEXT,
  response_json TEXT,
  data_json     TEXT,
  error_json    TEXT
);

CREATE UNIQUE INDEX uq_jobs_kind_key_active ON jobs(kind, key)
  WHERE key IS NOT NULL AND status IN ('pending', 'running', 'completed');
```

Note: `duration_ms` is a **Python property** on the `Job` dataclass, not a
stored column. Compute it from `started_at` / `completed_at` in SQL:

```sql
(julianday(completed_at) - julianday(started_at)) * 86400000 AS duration_ms
```

## Feeding history back to an LLM

Two surfaces:

**`Job.to_prompt()`** — render one job as Markdown:

```python
job = recorded.get(job_id)
print(job.to_prompt())
```

```
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

Slots that are `None` or empty don't render their section header — except
Request, which is the audit anchor and always prints. Response is gated on
`is not None` so legitimate falsy values (`0`, `False`, `[]`) still render
faithfully.

The CLI version: `python -m recorded get <job_id> --prompt`.

**Threading several turns**:

```python
recent = list(recorded.query(kind="agent.tool.*", status="completed", limit=10))
context = "\n\n".join(j.to_prompt() for j in reversed(recent))

# Hand `context` back to the model as conversation history
answer = llm.complete(f"{context}\n\nGiven the above, ...")
```

This pattern — record turns, query the recent ones, paste them back as
context — is the canonical agent-introspection loop. See
[`docs/examples/01_claude_turns.py`](../examples/01_claude_turns.py) for a
runnable version.

## Common queries

**Most recent failure for a given kind:**

```python
jobs = recorded.last(1, kind="orders.place", status="failed")
if jobs:
    print(jobs[0].error)
```

**All retries for a given idempotency key:**

```python
list(recorded.query(kind="orders.place", key="order-42", limit=20))
# Returns one currently-active row (pending/running/completed) plus all
# prior failed rows for this key — the partial unique index allows
# multiple `failed` rows per (kind, key).
```

**Throughput by hour for the last 24 hours:**

```python
conn = recorded.connection()
cur = conn.execute("""
    SELECT substr(submitted_at, 1, 13) AS hour,
           kind,
           COUNT(*) AS n
    FROM jobs
    WHERE submitted_at > datetime('now', '-1 day')
    GROUP BY hour, kind
    ORDER BY hour
""")
```

**Slow tail per kind:**

```python
conn.execute("""
    SELECT kind,
           (julianday(completed_at) - julianday(started_at)) * 86400000 AS duration_ms
    FROM jobs
    WHERE status = 'completed'
    ORDER BY duration_ms DESC
    LIMIT 50
""")
```
