# Changelog

Notable changes per release. Project follows [Semantic Versioning](https://semver.org/) once 1.0 is cut; pre-1.0 minor versions may include breaking changes if a clean dissolution warrants them.

## [Unreleased]

## [0.2.0] — 2026-05-07

Substrate discoverability and observability. No breaking changes; two
additive public symbols and a README/docs reframe driven by reader
feedback.

### Added

- **`recorded.health()`** — single-call store observability snapshot
  returning `db_size_mb`, `total_rows`, `oldest_row`, `newest_row`,
  `rows_last_hour`, `last_failed_at`, `leader_running`. Substrate-level:
  includes `_recorded.*` rows by design so health reflects the full
  store, not just user-visible function rows.
- **`recorded.copy_context_run`** — `contextvars.copy_context`-based
  helper for forwarding the recorder's `ContextVar` state across
  `ThreadPoolExecutor.submit` (and similar executor boundaries that
  don't propagate context automatically). Returns a wrapped callable
  bound to the calling thread's context.

### Documentation

- README reframed around the general pattern ("wrap any computation you
  want queryable and replayable") after two independent readers landed
  on a job-queue/ops-infrastructure read of the library. Lead example
  is now AST-parsing a file into a typed `FileMetrics` slot rather
  than `place_order`.
- New `docs/examples/05_codebase_scan.py` — pure-analysis worked
  example: walks `src/recorded/` itself, projects four AST metrics into
  a typed data slot, then queries the recorded space. `key=path:mtime`
  makes re-runs incremental.
- New `docs/usage/extension-patterns.md` — patterns for building on
  top of the substrate, with mixed-domain examples.
- `docs/usage/decorator.md` — ContextVar + `ThreadPoolExecutor`
  gotcha documented alongside `copy_context_run`; async-native
  recorder usage spelled out.
- `docs/usage/queries.md` — `strftime` composability on `submitted_at`
  for time-bucketed queries.
- `docs/examples/README.md` routes "I think this is a job queue"
  readers to `05_codebase_scan.py` first.

### Fixed

- `scripts/ci.sh` now runs `uv sync --extra dev` before invoking
  `ruff` / `ty` / `pytest`, so a fresh checkout doesn't fail at the
  first lint step.
- Stale `sqlite-api-job` path references in `docs/examples/01_*.py`
  and `02_*.py` corrected.

## [0.1.0] — 2026-04-28 — Initial release

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
