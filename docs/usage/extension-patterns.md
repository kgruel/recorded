# Extension patterns with `attach()`

`recorded` is deliberately small: one table, four typed slots, raw SQL access.
The extension point is `attach()` into the `data` slot plus your own
`ContextVar` state. That pattern lets you add domain-specific metadata without
changing `recorded`'s schema or asking the library to absorb your use case.

If your team can answer "what metadata will we query later?", `attach()` is the
place to put it.

## Pattern shape

1. Declare `@recorder(kind=..., data=Model)` for the queryable projection.
2. Compute your main response normally.
3. Call `attach("field", value)` for cross-cutting context that does not belong
   in the response payload.
4. Query later with `recorded.query(..., where_data={...})` or raw SQL.

This keeps response payloads faithful while making query keys explicit and
stable.

## Example 1: parent linking across request flow (APM-flavored)

```python
from contextvars import ContextVar
from pydantic import BaseModel
import recorded

parent_call_id: ContextVar[str | None] = ContextVar("parent_call_id", default=None)

class OutboundView(BaseModel):
    endpoint: str
    parent_id: str | None

@recorded.recorder(kind="api.outbound", data=OutboundView)
def call_partner(endpoint: str) -> dict:
    recorded.attach("endpoint", endpoint)
    recorded.attach("parent_id", parent_call_id.get())
    return http_client.get(endpoint)
```

## Example 2: feature pipeline lineage

```python
class ScoreView(BaseModel):
    account_id: str
    model_version: str

@recorded.recorder(kind="risk.score", data=ScoreView)
def score_account(account_id: str) -> dict:
    result = model.score(account_id)
    recorded.attach("account_id", account_id)
    recorded.attach("model_version", model.version)
    return result
```

Now model-rollout analysis is a query, not a code archaeology exercise.

## Example 3: scan-result tagging

```python
class ScanView(BaseModel):
    path: str
    severity: str

@recorded.recorder(kind="scan.file", data=ScanView)
def scan_file(path: str) -> dict:
    finding = scanner.run(path)
    recorded.attach("path", path)
    recorded.attach("severity", finding["severity_tier"])
    return finding
```

This keeps operational tags queryable without forcing every scanner output shape
into the same response schema.

## Example 4: per-call performance/concurrency snapshot

```python
class BrokerView(BaseModel):
    venue: str
    latency_ms: int
    inflight: int

@recorded.recorder(kind="broker.place", data=BrokerView)
def place(req: dict) -> dict:
    t0 = time.perf_counter()
    recorded.attach("inflight", inflight_counter.value())
    resp = broker.place(req)
    recorded.attach("venue", resp["venue"])
    recorded.attach("latency_ms", int((time.perf_counter() - t0) * 1000))
    return resp
```

The point is not "APM features in recorded"; the point is that you can build
your own diagnostics on top of a stable substrate.
