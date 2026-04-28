# Changelog

Notable changes per release. Project follows [Semantic Versioning](https://semver.org/) once 1.0 is cut; pre-1.0 minor versions may include breaking changes if a clean dissolution warrants them.

## [Unreleased]

## [0.1.0] — Initial release

First public release. The library records function calls to SQLite as a typed audit / idempotency log. Bare `@recorder` is the tier-1 surface; `.submit()` plus a separate leader process is the advanced tier.

### Core surface

- **`@recorder`** — transparent decorator. Removing it leaves working code with the same return-type and exception-type shape; only the side-effect goes away.
- **`key=`** for idempotency. Same key in another in-flight call joins the running work; same key after success returns the recorded response.
- **Typed slots** — `request=Model`, `response=Model`, `data=Model`, `error=Model`. Pydantic v2 (duck-typed; not imported by the library) and `@dataclass` both supported.
- **Read API** — `recorded.last`, `recorded.get`, `recorded.query`, `recorded.connection`. Module-level functions on a default Recorder; same surface available on a `Recorder()` instance.
- **`attach(key, value)`** — mid-execution annotation of the data slot. Strict against the declared `data=Model` schema (raises `AttachKeyError` on undeclared keys); free-form for bare `@recorder` without a model.
- **`attach_error(payload)`** — error-slot annotation on the failure path.
- **`.submit()` + `JobHandle`** — durable submission to a separate leader process. Requires running `python -m recorded run` (or equivalent). Documented as advanced; bare `@recorder` covers most use.
- **Optional FastAPI helper** — `recorded.fastapi.capture_request(...)` returns a serializable HTTP request envelope with header redaction and body-size cap.

### Named principles

These are documented in `docs/WHY.md` and govern design decisions in the library:

- **Wrap-transparency.** `@recorder` is a side effect, never a value transformation. The recorded variant of a function cannot raise an exception class the bare function couldn't have produced.
- **Audit invariant.** A row written via the public write path round-trips cleanly through the public read path.
- **Typed-slot contract.** A typed `data=Model` slot's declared field set is the contract; undeclared `attach()` keys raise at the call site, not at read time.
- **Status as persisted state.** `status` is a write-once-per-transition record of progress; staleness is inferable separately via `started_at` against `reaper_threshold_s`. The library deliberately doesn't expose real-time per-row liveness — the reaper plus a coarse threshold is sufficient.
- **Dissolution.** When a feature can be expressed as a property of what already exists, prefer that to adding a new subsystem.

### Operational

- Single-host SQLite WAL backend; `jobs.db` is the durable record.
- Stdlib-only core (no required runtime dependencies). Pydantic and FastAPI are duck-typed; both live behind `[dev]` extras.
- Python 3.10+ supported; CI matrix exercises 3.10 / 3.11 / 3.12 / 3.13.
- The reaper is a bootstrap-only sweep — runs once on `Recorder()` construction, cleaning up stale `running` rows from prior crashes. Long-lived processes won't sweep orphans created after their own startup.

### Privacy

The library persists arguments, return values, and exceptions to SQLite verbatim. `recorded.fastapi.capture_request(redact_headers=...)` redacts common secret-bearing HTTP headers; for general functions, redact sensitive arguments yourself before they cross the decorator boundary.

### Known limitations

- `recorded.query(where_data=...)` supports top-level equality only with three type-aware special cases (`None` matches via `IS NULL`; bool matches via `json_type`). Nested paths, ranges, `IN`, and `LIKE` require dropping to `recorded.connection()` and raw SQL.
- A leader process death is not auto-recovered for long-lived non-leader Recorders. Operationally: run the leader under a supervisor that restarts it on a cadence bounding your tolerance for stuck rows.
- Pydantic models declaring field aliases without `populate_by_name=True` (or `validate_by_name=True` for v2.11+) are refused at decorator-evaluation time — they'd write canonical names but fail to rehydrate, breaking the audit invariant. The error message names the offending field and the one-line config fix.
