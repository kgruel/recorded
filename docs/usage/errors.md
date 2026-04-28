# Errors

All library-raised exceptions inherit from `RecordedError`. They're
re-exported from the top-level `recorded` namespace, so
`except recorded.X:` works without reaching into private modules.

```python
import recorded

try:
    reply = place_order(req, key="order-42")
except recorded.JoinedSiblingFailedError:
    ...
except recorded.JoinTimeoutError:
    ...
```

The wrapped function's own exceptions propagate verbatim. The library
records them and re-raises — it does not wrap user-domain errors.

## The hierarchy

```
RecordedError                         # root for library-raised errors
├── UsageError                        # caller used the API wrong
│   ├── ConfigurationError            # bad decorator config / call shape
│   ├── SyncInLoopError               # .sync() inside a running loop
│   ├── RecorderClosedError           # operation on a shut-down Recorder
│   └── SerializationError            # value couldn't fit in a typed slot
└── IdempotencyError                  # idempotency-keyed call failures
    ├── JoinedSiblingFailedError      # joined a failed sibling row
    ├── JoinTimeoutError              # also inherits stdlib TimeoutError
    └── IdempotencyRaceError          # rare lookup-race; safe to retry
```

Two main subtrees:

- **`UsageError`** — programming or configuration mistakes the caller should
  fix in their code. Catch this when you want any "you used the API wrong"
  error.
- **`IdempotencyError`** — failures specific to idempotent (`key=`) calls.
  Catch this when you want to handle idempotency-specific outcomes uniformly.

`JoinTimeoutError` multi-inherits stdlib `TimeoutError` so
`except TimeoutError:` still catches it.

## When each fires

### `ConfigurationError`

Bad decorator setup, call shape, or operational state. Examples:

- `key=` passed at a call site whose decorator has an auto-derived `kind`.
  Idempotency requires explicit `kind=`. See
  [idempotency](idempotency.md#key-requires-explicit-kind).
- A `request=`/`response=`/`data=`/`error=` model is neither a dataclass
  nor a Pydantic v2 model.
- `request=Model` declared but the call passes multiple positional /
  keyword arguments. The library can't tell which arg is the model
  instance — declare a single explicit request parameter.
- `kind="_recorded.*"` — reserved prefix; user code can't decorate with it.
- `where_data` key contains `.` or `$` (nested-path attempt). Use
  `recorded.connection()` for richer queries.
- **`.submit()` called with no leader process running.** Start
  `python -m recorded run --path <jobs.db> --import <module>` as a
  sibling process, or call the function bare (without `.submit()`).
  Probe with `recorder.is_leader_running()` for health checks.

Always raised at decoration time or call time, never on the recording
path.

### `SyncInLoopError`

`.sync()` invoked from inside a running event loop. Calling `asyncio.run`
under an already-running loop would deadlock or silently misbehave. Same
exception fires for `JobHandle.wait_sync()` from inside a loop.

Fix: use `await fn(...)` from the async context, or move the call to a
synchronous entry point.

### `RecorderClosedError`

An operation was attempted on a `Recorder` whose `shutdown()` has run.
Most relevant to test fixtures and explicit-instance code paths — the
configured default doesn't normally see this because `atexit` runs at
the end.

### `SerializationError`

A value couldn't be serialized into a typed slot. Two reasons:

- A value passed to a typed slot doesn't satisfy the registered model.
  E.g., decorator declares `response=OrderReply` but the function
  returned a dict missing required fields.
- The function's return value is not JSON-serializable and not auto-
  renderable (not a dataclass, not Pydantic).

Carries `slot=`, `model=`, `value=` attributes for diagnosis:

```python
try:
    reply = place_order(req)
except recorded.SerializationError as e:
    print(e.slot)    # "response"
    print(e.model)   # OrderReply
    print(e.value)   # the offending dict
```

The bare-call path (without `key=`) treats post-success serialization
failures as a side-channel signal: the row is marked failed, a warning is
logged on the `recorded` logger, and the wrapped function's natural value
is still returned to the caller. Wrap-transparency requires bare-call
surfaces never raise an exception class the wrapped function wouldn't.

### `JoinedSiblingFailedError`

An idempotency-keyed call joined a sibling row that terminated as failed.

Fires from:

- Bare-call `key=` paths when joining a failed row (only with
  `retry_failed=False`).
- `JobHandle.wait()` / `wait_sync()` when the awaited row terminated as
  failed.
- `.submit(key=..., retry_failed=False)` where the latest active row is
  failed — the returned handle's `.wait()` raises.

Carries `kind`, `key`, `sibling_job_id`, `sibling_error`:

```python
try:
    place_order(req, key="order-42", retry_failed=False)
except recorded.JoinedSiblingFailedError as e:
    print(e.kind)             # "orders.place"
    print(e.key)              # "order-42"
    print(e.sibling_job_id)   # job id of the failed row
    print(e.sibling_error)    # raw dict from error_json
```

`sibling_error` is always the raw decoded dict, not the rehydrated typed
model — same shape regardless of whether `error=Model` was registered.

### `JoinTimeoutError`

Idempotency join (or `JobHandle.wait()`) didn't reach terminal status
within the timeout. Default 30 s, override via
`recorded.configure(join_timeout_s=...)` or per-call `wait(timeout=...)`.

Carries `kind`, `key`, `sibling_job_id`, `timeout_s`. Multi-inherits
stdlib `TimeoutError`, so `except TimeoutError:` catches it.

The leader's row is left untouched — your timeout is local; the leader
can still complete and unblock other joiners.

### `IdempotencyRaceError`

Rare. The `(kind, key)` row was active when we pre-checked, our `INSERT`
raised `IntegrityError`, but by the time we re-looked-up the active row
it was gone (the prior holder failed and was reaped between our INSERT
and our lookup).

The library raises `IdempotencyRaceError` and tells the caller to retry.
In practice you'll see this only under sustained contention with very
short-lived failures. Carries `kind` and `key`.

## What to catch where

**For your own retry logic on a single idempotent call:**

```python
try:
    reply = place_order(req, key=k)
except recorded.IdempotencyError:
    # All idempotency outcomes: failed sibling, timeout, race
    log_and_retry()
```

**For "I made a mistake configuring the API":**

```python
try:
    place_order(req, key=k)
except recorded.UsageError:
    # ConfigurationError / SyncInLoopError / RecorderClosedError /
    # SerializationError
    fix_my_code()
```

**For surfacing a failed sibling specifically (e.g. `retry_failed=False`):**

```python
try:
    reply = place_order(req, key=k, retry_failed=False)
except recorded.JoinedSiblingFailedError as e:
    notify_team(e.sibling_error)
```

**For network-style timeout handling:**

```python
try:
    job = await handle.wait(timeout=10.0)
except TimeoutError:    # catches JoinTimeoutError
    fall_back()
```

**Library-wide catch-all (rare):**

```python
try:
    ...
except recorded.RecordedError:
    # Anything the library raised. Doesn't catch user-domain exceptions
    # from the wrapped function — those propagate verbatim.
```

## Wrapped-function exceptions

The wrapped function's own exceptions are **recorded and re-raised
verbatim**. They are not wrapped in `RecordedError`. If your function
raises `httpx.HTTPStatusError`, the call site sees `httpx.HTTPStatusError`,
unchanged.

This is part of the wrap-transparency invariant: removing `@recorder` from
the function does not change the exception types your callers handle.

> Architecture context: [`docs/WHY.md` — wrap-transparency](../WHY.md#wrap-transparency)
