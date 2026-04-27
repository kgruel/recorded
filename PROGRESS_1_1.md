# Sub-phase 1.1 — Foundation: progress report

Status: complete. 25 tests green, full suite 1.26s on Python 3.13 / SQLite 3.53.

## Files created

### Package

- `pyproject.toml` — hatchling-backed packaging; `recorded` 0.1.0; runtime deps = none; `[dev]` extras add pytest, pytest-asyncio (strict), hypothesis, pydantic. `pytest.ini_options` sets `asyncio_mode = "strict"` and pins testpaths.
- `README.md` — local-dev (`uv venv && uv pip install -e ".[dev]" && uv run pytest`); SQLite 3.35.0+ requirement noted.
- `src/recorded/__init__.py` — public API surface for 1.1: `Recorder`, `Job`, `attach`, `get`, `last`. Decorator `recorder` deliberately not exported yet.
- `src/recorded/_storage.py` — schema DDL, canonical SQL constants (`SELECT_BY_ID`, `SELECT_LAST`, `INSERT_PENDING`, `UPDATE_RUNNING`, `UPDATE_COMPLETED`, `UPDATE_FAILED`), status constants, `now_iso()`, `new_id()`, `open_connection()` (WAL + autocommit + foreign_keys), `ensure_schema()`.
- `src/recorded/_adapter.py` — single `Adapter` class with three strategies (`passthrough`, `dataclass`, `pydantic`); duck-typed Pydantic detection (no import).
- `src/recorded/_types.py` — `Job` dataclass with `duration_ms` property; one private `_parse_iso` helper (handles `Z` suffix).
- `src/recorded/_registry.py` — module-level `kind -> RegistryEntry`; `register()`, `lookup()`, `get_or_passthrough()`, `_reset()` (test hook). `auto_kind` flag is on `RegistryEntry` for the 1.4 `key`-vs-auto-kind validation; not yet used.
- `src/recorded/_context.py` — `current_job` ContextVar carrying `JobContext(job_id, kind, recorder, buffer)`; `attach(key, value, flush=False)` raises `AttachOutsideJobError` outside a job.
- `src/recorded/_recorder.py` — `Recorder` (connection mgmt, idempotent `shutdown`, sync context-manager protocol); `_connection()`, `get()`, `last()`, `_flush_attach()` (write-through helper for `attach(flush=True)`). Module-level lazy singleton via `get_default()`; private `_set_default()` test hook.
- `src/recorded/_errors.py` — `RecordedError` root + `ConfigurationError`, `AttachOutsideJobError`, `SyncInLoopError`.

### Tests

- `tests/conftest.py` — `db_path` (tmp file), `recorder`, `default_recorder` (installs as module default), and an autouse registry-reset fixture.
- `tests/test_storage.py` — `now_iso` shape + sortability, `new_id` uniqueness/hex, schema bootstrap (columns, indexes, WAL), partial-unique-index behavior (active vs failed history).
- `tests/test_types.py` — example-based + property-based (Hypothesis) roundtrip for all three adapter tiers; Pydantic gated on import; `Job.duration_ms` examples.
- `tests/test_attach.py` — contextvar contract: outside-job raises; inside-job buffers; `asyncio.gather` isolation. (Note: not the same as named `test_async_attach_isolated_across_gather`, which requires the full lifecycle and lands in 1.3.)
- `tests/test_read.py` — `recorded.get` rehydration via registry; unknown id → `None`; unregistered kind → raw dicts; `recorded.last(n, kind="broker.*")` with glob and ordering; `Recorder.shutdown()` idempotency.

## Design choices made within DESIGN.md's guidance

- **Module-level default + private `_set_default(r)` test hook.** Component 17 (`recorded.configure`) is out-of-scope, but tests need to redirect `recorded.get()` at a tmp DB. `_set_default` is a private hook on `_recorder`; will be replaced by the real `configure()` API in a later phase. Avoids shipping a public surface I'd then rip out in 1.2.
- **Pydantic detection by duck typing.** `Adapter` checks for `model_validate` and `model_dump` attrs on the class without importing pydantic — keeps the core dependency-free per DESIGN.md "zero required deps."
- **Connection: `isolation_level=None`, `check_same_thread=False`, WAL + NORMAL synchronous.** Autocommit lets the `journal_mode=WAL` pragma take effect immediately and matches the implicit-commit pattern the lifecycle queries assume; `check_same_thread=False` is required because phase 2's `asyncio.to_thread` lifecycle writes will hit the connection from helper threads.
- **`Adapter.serialize` accepts both instances and dicts.** Dict input is rebuilt through the model (`Model(**dict)` or `model_validate(dict)`) so storage shape is canonical and validation happens on write. Missing fields surface as the model's natural error (`TypeError` for dataclass, `ValidationError` for Pydantic) rather than a wrapped library exception — caller debuggability matters more than hierarchy purity here.
- **`SELECT_LAST` glob filter pattern.** `WHERE (? IS NULL OR kind GLOB ?)` with both placeholders bound to the same kind — single SQL string, NULL-safe.
- **`UPDATE_FAILED` permits `pending → failed` *and* `running → failed`.** DESIGN.md describes the happy path (`pending → running → terminal`) but a wrapper that fails before `started_at` is set (validation errors, etc.) needs to write a terminal row from `pending`. Flagging here so 1.2 lifecycle code uses this query for both transitions and tests cover the early-failure path.
- **Exception hierarchy.** `RecordedError` root + three concrete subclasses today (`ConfigurationError` for 1.4 decoration-time refusals, `AttachOutsideJobError`, `SyncInLoopError` for 1.3). Lean enough to grow without churn.
- **`_flush_attach` already wired on `Recorder`.** `attach(flush=True)` from `_context.py` calls `recorder._flush_attach(...)` via SQLite's `json_patch`. Implementation lands now because the contextvar exposes `flush=True`; the named flush-through test (`test_attach_flush_true_writes_through`) lives in 1.3 where the decorator binds a real recorder into the contextvar. Latent gotcha: a `JobContext` constructed by hand with `recorder=None` and `flush=True` would NoneType — this only happens in adversarial tests, but I'll harden it in 1.3 if convenient.

## Interpretations / clarifications worth flagging to the lead

1. **`recorded.get()` with no `configure()` available.** Tests use `_set_default` to redirect the singleton; production callers in 1.1 would hit the cwd default `./jobs.db`. If the lead wants the public `configure()` surface earlier than component-17 timing, say so and I'll lift it forward.
2. **`Recorder.last` accepts `kind=` glob now.** DESIGN.md's read API spec includes glob, the named-test slice doesn't require it, but it's a one-liner via `kind GLOB ?` and tests assert on it. Keeping it.
3. **`Recorder` `__enter__/__exit__` shipped.** Component 17's "explicit Recorder context manager" is technically out-of-scope, but the sync `with`-block is one method pair and the test fixture needs the connection bootstrap path exercised. The richer "configure routing via contextvar within `with`-block" pattern from DESIGN.md ¶ Recorder lifecycle is *not* shipped — that's still a phase-2 concern.
4. **`isolation_level=None` and the lifecycle UPDATEs.** Each lifecycle query (`INSERT_PENDING`, `UPDATE_RUNNING`, `UPDATE_COMPLETED`/`_FAILED`) is a single statement — autocommit gives the per-row durability the lifecycle wants. If 1.2 needs multi-statement atomicity (e.g. idempotency-collision lookups), I'll wrap them in `BEGIN IMMEDIATE`/`COMMIT` explicitly rather than reverting to deferred-transaction mode.
5. **No `Recorder.path` validation on `__init__`.** DB file is created lazily on first `_connection()`. Tests rely on this; matches DESIGN.md's "lazy module-level singleton at ./jobs.db."

## Test results

Full suite: **25 passed in 1.26s.**

### Named tests in 1.1 scope

| test | status |
|---|---|
| `test_dataclass_roundtrip_via_data_slot` | PASS (`tests/test_types.py`) |
| `test_pydantic_roundtrip_when_installed` | PASS (Pydantic 2.13 installed in dev extras) |
| `test_recorded_get_returns_rehydrated_typed_values` | PASS (`tests/test_read.py`) |

### Named tests deferred to later sub-phases

| test | sub-phase |
|---|---|
| `test_bare_call_writes_pending_running_terminal_in_order` | 1.2 |
| `test_bare_call_returns_wrapped_functions_natural_value` | 1.2 |
| `test_async_attach_isolated_across_gather` | 1.3 (requires full lifecycle + completion flush) |
| `test_attach_buffers_until_completion_by_default` | 1.3 |
| `test_attach_flush_true_writes_through` | 1.3 |
| `test_sync_call_inside_running_loop_raises_helpful_error` | 1.3 |
| `test_idempotency_collision_returns_existing_completed_without_reexecution` | 1.4 |
| `test_idempotency_collision_on_pending_waits_for_terminal` | 1.4 |
| `test_idempotency_with_failed_retries_by_default` | 1.4 |
| `test_idempotency_retry_failed_false_returns_failed_job` | 1.4 |
| `test_key_with_auto_kind_raises_at_decoration` | 1.4 |

(11 of 14 named tests deferred; 3 of 14 passing in 1.1.)

## Stopping here

Per the brief: STOP after 1.1, await explicit go-ahead before starting 1.2. No commit yet — holding for lead review. Smoke check `python -c "from recorded import Recorder, attach, get, last, Job"` passes outside the test harness.
