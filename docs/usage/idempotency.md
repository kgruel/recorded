# Idempotency keys

Pass `key=` at the call site to make a call idempotent. If a job with the
same `(kind, key)` is already pending, running, or completed, the second
call **joins** the existing row rather than re-running the function.

This is the feature you reach for when calling APIs that charge per request,
make external state changes, or shouldn't double-fire on retries.

> Architecture context: [`docs/HOW.md` — the partial-unique-index trick](../HOW.md#idempotency--the-partial-unique-index-trick)

## The basic shape

```python
@recorder(kind="orders.place")
def place_order(req: OrderRequest) -> OrderReply:
    return broker.place(req)

# First call runs the function and records the result
reply = place_order(req, key="order-42")

# Subsequent calls with the same key return the cached response
# without re-charging the broker — even from a different process.
reply = place_order(req, key="order-42")
```

`key=` is reserved at every call surface (`fn(...)`, `fn.sync(...)`,
`fn.async_run(...)`, `fn.submit(...)`) — it's stripped before the wrapped
function runs.

## How it works

Every `@recorder` table has a partial unique index:

```sql
CREATE UNIQUE INDEX uq_jobs_kind_key_active ON jobs(kind, key)
  WHERE key IS NOT NULL AND status IN ('pending', 'running', 'completed');
```

The index allows multiple `failed` rows for the same `(kind, key)` (history
preserved) but at most **one** active row at a time.

A keyed call follows this flow:

1. **Pre-INSERT lookup**: is there an active row for `(kind, key)`?
   - `completed` → return its response immediately.
   - `pending`/`running` → fall through to wait.
   - `failed` + `retry_failed=True` (default) → proceed to INSERT.
   - `failed` + `retry_failed=False` → raise `JoinedSiblingFailedError`.
2. **`INSERT`**: if it succeeds, we own this `(kind, key)` slot.
3. **If `INSERT` raises `IntegrityError`**: we lost the race. Look up the
   winning row and wait on it via the wait primitive.

In-process joins resolve via a `concurrent.futures.Future` registered under
the recorder's notify lock — no polling. Cross-process joins fall back to a
short polling loop (200 ms cadence) with the same wait helper.

## `key=` requires explicit `kind=`

```python
@recorder                        # auto-derived kind
def place_order(req): ...

place_order(req, key="order-42")
# raises ConfigurationError: "place_order uses an auto-derived kind...
#  Idempotency keys require an explicit kind to remain stable across renames."
```

The reasoning: the auto-derived kind is `f"{fn.__module__}.{fn.__qualname__}"`.
If you rename or move the function, the kind changes — and an `(old_kind,
key)` row no longer collides with a `(new_kind, key)` insert. That defeats
the whole point of idempotency. The library refuses-to-compile rather than
let you accidentally lose deduplication on a refactor.

Fix:

```python
@recorder(kind="orders.place")   # explicit, survives rename
def place_order(req): ...
```

## `retry_failed=` semantics

When a prior call with the same `(kind, key)` already failed, the next call
faces a choice: retry, or surface the prior failure?

```python
# default: retry_failed=True — retry by inserting a fresh row
place_order(req, key="order-42")

# opt out: surface the prior failure rather than retrying
place_order(req, key="order-42", retry_failed=False)
```

With `retry_failed=False`, the call raises `JoinedSiblingFailedError`
carrying the prior row's recorded error payload. The same exception is
raised regardless of whether the failure happened in this process or a
different one.

`retry_failed` does **not** affect joining a currently-pending or
currently-running row — those always join. The flag only changes the
behavior when the latest active row is `failed`.

## `JoinedSiblingFailedError`

```python
try:
    reply = place_order(req, key="order-42", retry_failed=False)
except recorded.JoinedSiblingFailedError as e:
    print(e.kind)             # "orders.place"
    print(e.key)              # "order-42"
    print(e.sibling_job_id)   # full job id of the failed row
    print(e.sibling_error)    # raw dict from error_json
```

`sibling_error` is always the raw decoded `error_json` dict, regardless of
whether `error=Model` was registered for the kind. This is so callers
programmatically branching on the error see the same shape across decorators.

To reach the rehydrated typed error model, fetch the sibling row through the
read API:

```python
try:
    place_order(req, key="order-42", retry_failed=False)
except recorded.JoinedSiblingFailedError as e:
    sibling = recorded.get(e.sibling_job_id)
    typed_error = sibling.error  # rehydrated to error= model if registered
```

## Wrap-transparency on the failure path

Keyed-call surfaces follow the **wrap-transparency invariant**: they always
either return the response or raise. They never return a `Job` representing
a failed sibling.

This applies uniformly:

- bare `place_order(req, key="...")` — returns response or raises.
- `place_order.submit(req, key="...")` returning a `JobHandle`, then
  `handle.wait()` — returns `Job` (success) or raises.
- `retry_failed=False` joining a prior failed row — raises.

To inspect prior failed rows, use the read API. `last()` doesn't filter by
`key`, so use `list()`:

```python
list(recorded.list(
    kind="orders.place", key="order-42", status="failed", limit=10,
))
```

## `JoinTimeoutError`

Idempotency joins block on the wait primitive. If the leader doesn't finish
within `join_timeout_s` (default 30 s), the joiner raises `JoinTimeoutError`:

```python
try:
    reply = place_order(req, key="order-42")
except recorded.JoinTimeoutError as e:
    print(e.timeout_s)         # 30.0
    print(e.sibling_job_id)    # the leader's job id
```

`JoinTimeoutError` multi-inherits stdlib `TimeoutError`, so
`except TimeoutError:` catches it too. The leader's row is left untouched —
your timeout is local; the leader can still complete and unblock other
joiners.

Override per-recorder via `recorded.configure(join_timeout_s=...)`. There's
no per-call timeout for bare-call joins — use `.submit()` if you need
per-call timeout control (`JobHandle.wait(timeout=...)`).

## `IdempotencyRaceError`

Practically rare. Reachable only if:

1. We pre-checked: no active row.
2. We tried to `INSERT`: `IntegrityError` (someone else got there first).
3. We re-checked for the active row: it's gone (the holder failed and
   was reaped or cleaned up between steps 2 and 3).

The library raises `IdempotencyRaceError` and tells the caller to retry. In
practice, you'll see this only under sustained contention with very short-
lived failures.

## Multi-process safety

SQLite WAL handles concurrent writers across processes — the partial unique
index enforces "at most one active row per `(kind, key)`" at the storage
layer. Two processes simultaneously calling `place_order(req, key="K")`:

- One INSERT wins, that process runs the function.
- The other INSERT raises `IntegrityError`; that process joins via the wait
  primitive (cross-process polling fallback, 200 ms cadence).

Both callers receive the same response.

## Observing the live row

While a leader is running, the active row sits at status `pending` (briefly,
for `.submit()` calls) or `running`. You can watch it from another process:

```bash
python -m recorded last 5 --kind 'orders.*' --status running
```

Once terminal, the row's status flips to `completed` or `failed` and any
in-process subscribers resolve immediately via the notify primitive.
