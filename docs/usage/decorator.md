# `@recorder` — the decorator

The decorator is the spine of `recorded`. Every other feature — idempotency,
background execution, typed slots — layers on top of a function that's been
wrapped with `@recorder`.

> Architecture context: [`docs/OVERVIEW.md` — trace a call](../OVERVIEW.md#trace-a-call-bare-path)

## Bare usage

```python
from recorded import recorder

@recorder
def fetch_quote(symbol: str) -> dict:
    return http.get(f"/quotes/{symbol}").json()

fetch_quote("AAPL")  # records and returns the dict
```

With no arguments, `kind` is auto-derived from the function's qualified name:
`f"{fn.__module__}.{fn.__qualname__}"` — e.g. `myapp.broker.fetch_quote`.
That's fine for ad-hoc use, but renaming or moving the function changes the
`kind`, which makes auto-kinds unsuitable for `key=` (see
[idempotency](idempotency.md)).

## Factory usage

```python
@recorder(kind="broker.fetch_quote")
def fetch_quote(symbol: str) -> dict:
    ...
```

With `kind=` set explicitly, the kind survives renames and refactors. Any time
you plan to use idempotency keys, query history by kind, or persist data across
versions, declare `kind=` explicitly.

## Decorator parameters

| parameter | purpose |
|---|---|
| `kind: str \| None` | logical name for the call (`"broker.fetch_quote"`). Required for `key=` use; auto-derived from `fn.__module__.fn.__qualname__` if omitted. |
| `request: type \| None` | model for the `request` slot — Pydantic v2 or `@dataclass`. |
| `response: type \| None` | model for the `response` slot. |
| `data: type \| None` | model for the `data` slot (queryable projection). |
| `error: type \| None` | model for the `error` slot. |

All four model parameters are independent. You can declare any subset.
See [typed slots](typed-slots.md) for the full slot story.

## Sync vs async

The decorator inspects the wrapped function and preserves its calling
convention:

```python
@recorder(kind="db.read")
def read_sync(id: str) -> Row:        # decorated sync stays sync
    return ...

@recorder(kind="api.fetch")
async def fetch_async(url: str) -> dict:   # decorated async stays async
    return ...

read_sync("abc")          # call as before
await fetch_async("...")  # await as before
```

Async wrappers run all SQLite I/O via `asyncio.to_thread`, so the loop never
blocks on disk.

## Cross-mode shims

A wrapped function exposes `.sync` and `.async_run` so a caller in the "wrong"
calling mode can still invoke it:

```python
@recorder(kind="api.fetch")
async def fetch(url: str) -> dict:
    ...

# From a sync entry point: run the coroutine to completion.
result = fetch.sync("https://...")

# From an async caller: same as `await fetch(...)`.
result = await fetch.async_run("https://...")
```

For a sync-decorated function called from async code, `.async_run` runs the
function on the threadpool via `asyncio.to_thread` so it doesn't block the
loop. ContextVars (and therefore `attach()` / `attach_error()`) propagate
through `to_thread`.

`.sync()` from inside a running event loop raises `SyncInLoopError` — running
`asyncio.run` while a loop is already running would deadlock or silently
misbehave. Use `await fn(...)` instead.

## Call-site parameters

Every wrapped call accepts two reserved kwargs:

| kwarg | default | purpose |
|---|---|---|
| `key=` | `None` | idempotency key — see [idempotency](idempotency.md). Requires explicit `kind=` on the decorator. |
| `retry_failed=` | `True` | when joining a prior failed row by `key=`, whether to retry (`True`) or surface the failure (`False`). |

These are stripped before the wrapped function runs — your function never sees
them in its `**kwargs`.

## What the wrapper preserves

- **Return type**: bare calls return the wrapped function's natural value.
  Adding or removing `@recorder` doesn't change the call-site type.
- **Exception type**: failures re-raise the wrapped function's original
  exception. The library never wraps user-domain errors.
- **Calling convention**: sync stays sync, async stays async.

This is the **wrap-transparency invariant**: if you ripped out `@recorder`,
your call sites would still work. The only differences vanish — the audit
trail and the keyed-call surfaces (`.submit()`, `JobHandle.wait()`,
`JoinedSiblingFailedError`).

> Architecture context: [`docs/DESIGN.md` — wrap-transparency, scoped to basic](../DESIGN.md#2-wrap-transparency-scoped-to-basic)

## What the wrapper adds

The wrapper also exposes three call-mode attributes:

| attribute | shape |
|---|---|
| `wrapper.sync(*args, **kw)` | invoke as sync, returning the natural value. |
| `wrapper.async_run(*args, **kw)` | invoke as async, returning a coroutine. |
| `wrapper.submit(*args, key=..., **kw)` | enqueue a `pending` row, return a `JobHandle`. See [workers](workers.md). |

Plus diagnostic attributes: `wrapper.kind`, `wrapper._entry`,
`wrapper._is_async`. The leading-underscore ones are not part of the API
surface — use them for debugging only.
