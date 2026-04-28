# Typed slots

A `recorded` row has four JSON columns: `request`, `response`, `data`,
`error`. Each is a **slot** — a JSON column optionally validated by a typed
model. Adding type definitions buys you input validation, structured query
shapes, and rehydrated typed objects on the read side.

> Architecture context: [`docs/HOW.md` — the slot primitive](../HOW.md#the-single-primitive-the-slot)

## The four slots

| slot | what goes in it |
|---|---|
| `request` | what the caller passed (positional + keyword args) |
| `response` | what the function returned |
| `data` | a queryable projection plus caller-attached side data |
| `error` | failure shape (when `status == "failed"`) |

Each slot is independent. You can declare none, any one, or all four.

## Untyped (passthrough) — the default

```python
@recorder(kind="api.fetch")
def fetch(url: str) -> dict:
    return http.get(url).json()
```

With no model declarations, every slot is **passthrough**: values are stored
as JSON-native types (dicts, lists, strings, numbers, booleans, None) without
validation. Reads return raw dicts.

The passthrough adapter has one auto-rendering convenience: dataclass and
Pydantic instances are dumped to their JSON-native form before storage.
Without this, returning a dataclass from a `@recorder` function with no
`response=Model` would crash `json.dumps` and fail the row — violating the
audit invariant. With it, typed-instance returns survive and land as their
dumped dict.

```python
@dataclass
class OrderReply:
    order_id: str

@recorder(kind="orders.place")
def place(req) -> OrderReply:
    return OrderReply(order_id="ord-9281")

place(req)
# Row recorded; response_json = {"order_id": "ord-9281"}
# (auto-rendered even though no response= was declared.)
```

Container types (`list[Model]`, `dict[str, Model]`) are not auto-walked —
wrap in a response model with a typed list field if you need that.

## Typed slots

```python
from pydantic import BaseModel

class OrderRequest(BaseModel):
    symbol: str
    qty: int

class OrderReply(BaseModel):
    order_id: str
    filled_at: str

@recorder(
    kind="orders.place",
    request=OrderRequest,
    response=OrderReply,
)
def place_order(req: OrderRequest) -> OrderReply:
    return broker.place(req)
```

What changes when a model is declared:

- **At call time**: the value is validated through the model. A `dict` is
  validated by construction (`Model(**dict_value)`); a model instance is
  validated by `isinstance` check or re-validated.
- **At storage time**: the validated model is dumped to JSON-native form
  via `model_dump(mode="json")` (Pydantic) or `dataclasses.asdict`.
- **At read time**: the raw dict is rehydrated back to a model instance —
  `job.request` is an `OrderRequest`, `job.response` is an `OrderReply`.

If a value can't be validated, the call raises `SerializationError` with
`slot=`, `model=`, and `value=` attributes for diagnosis.

## Supported model types

Three flavors, detected by duck typing:

1. **Pydantic v2** — anything with `model_validate` and `model_dump` callables.
   The library never imports `pydantic`; if your class quacks v2, it works.
2. **`@dataclass`** — stdlib `dataclasses.dataclass`. Validates by
   `Model(**value)` construction; dumps via `dataclasses.asdict`.
3. **`None`** — passthrough. The default.

Anything else passed to `request=` / `response=` / `data=` / `error=` raises
`ConfigurationError` at decoration:

```python
@recorder(kind="bad", request=str)   # str is a type but not dataclass/pydantic
# ConfigurationError: Unsupported model type: <class 'str'>...
```

## The `data` slot — queryable projections

`data` is the slot that turns the audit log into a queryable index. It
accepts:

- A `data=Model` declaration plus auto-projection from the response
  (the response is dumped, then the data model is constructed from
  the projection's keys).
- Mid-execution `attach(key, value)` calls to merge in side data.
- Both at once.

```python
class OrderView(BaseModel):
    """A queryable projection: only the keys you'll filter by."""
    order_id: str
    symbol: str
    customer_id: int

@recorder(
    kind="orders.place",
    response=OrderReply,
    data=OrderView,
)
def place_order(req) -> OrderReply:
    return broker.place(req)

# Now:
recorded.list(
    kind="orders.place",
    where_data={"customer_id": 7},
)
# Filters by data.customer_id efficiently — see queries.md.
```

Auto-projection fires in two cases:

- **The response is an instance of the `data` model** — dumped directly.
- **The response is a `dict` the `data` model can validate** — Pydantic allows
  extra fields by default, so a wide response dict that contains the model's
  fields projects cleanly. Dataclass adapters filter to declared field names
  before construction.

Anything else (a different model with overlapping field names, a primitive,
an unrelated dict) doesn't project — `data_json` is left to whatever
`attach()` populated. When a `data` slot is declared but the projection
ends up empty, the recorder emits a deduped warning on the `recorded`
logger (one per `(kind, reason)` per process) so silent drift is visible.
Disable via `Recorder(warn_on_data_drift=False)` or
`configure(warn_on_data_drift=False)`. The fix is usually one of: return an
instance of the `data` model, or call `attach({...})` with the queryable
slice you want.

## `attach()` — mid-execution annotations

```python
from recorded import attach

@recorder(kind="orders.place", data=OrderView)
def place_order(req: OrderRequest) -> OrderReply:
    attach("broker_latency_ms", measure_latency())
    attach("provider", "alpaca")
    return broker.place(req)
```

`attach(key, value)` writes to an in-memory buffer that is merged into the
`data` slot at the terminal write. `flush=True` writes through immediately
via `json_patch` (useful if the function might crash before completion):

```python
attach("attempt", 1, flush=True)
result = risky_call()  # if this crashes, attempt=1 is already in data_json
```

Multiple `attach()` calls with different keys merge cleanly. Same key:
last-write-wins.

`attach()` is a **silent no-op outside an active recorded context**. This is
intentional — removing `@recorder` from a function shouldn't require also
deleting every `attach()` call in its body.

## `attach_error()` — structured error payloads

`attach_error()` writes to the `error` slot on the failure path:

```python
from recorded import attach, attach_error

class BrokerError(BaseModel):
    code: str
    request_id: str
    upstream_message: str

@recorder(kind="orders.place", error=BrokerError)
def place_order(req):
    try:
        return broker.place(req)
    except broker.RateLimit as e:
        attach_error(BrokerError(
            code="rate_limit",
            request_id=e.request_id,
            upstream_message=str(e),
        ))
        raise
```

`attach_error(payload)` semantics:

- **Full-replace**: each call overwrites the error buffer (last-write-wins).
- **Validated through `error=Model` if registered**: the payload goes
  through `error.serialize()` like any other typed slot.
- **Without `attach_error()`**: the recording layer falls back to
  `{type, message}` from the original exception. This means decorating with
  `error=Model` but not calling `attach_error()` will gracefully degrade —
  the model's `model_validate({"type": ..., "message": ...})` runs.
- **The wrapped function's exception still propagates verbatim**.
  `attach_error()` only re-shapes the *recording*, not the raise.

Like `attach()`, `attach_error()` is a silent no-op outside an active
context.

## The audit invariant

The raw response is **always recorded** unless the caller explicitly opts
into a lossy `response=Model` projection. With `response=Model` declared,
keys not on the model are dropped — that's the lossy opt-in.

Without `response=Model`, the passthrough adapter records what the function
returned, auto-rendering typed instances if needed. This is the audit
faithfulness guarantee: the row reflects what actually happened.

If recording itself fails post-success (e.g. response is non-JSON-serializable
and not auto-renderable), the row is marked failed, a warning is logged on
the `recorded` logger, and the wrapped function's natural value is still
returned to the caller — wrap-transparency requires bare-call surfaces never
raise an exception class the wrapped function wouldn't.

> Architecture context: [`docs/WHY.md` — wrap-transparency](../WHY.md#wrap-transparency)
> · [`docs/WHY.md` — mid-execution attach](../WHY.md#contextvars-for-attach)
