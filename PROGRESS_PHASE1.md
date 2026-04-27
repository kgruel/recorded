# Phase 1 — Completion report

Status: complete. **49 tests, 0.46 s** suite runtime on Python 3.13 / SQLite 3.53.
All 14 named tests collected and passing.
Stress-checked: the gather-race idempotency test passed 50/50 in succession.

## Files

### Package (`src/recorded/`)

| file | role |
|---|---|
| `__init__.py` | Public surface: `recorder`, `Recorder`, `Job`, `attach`, `get`, `last`. |
| `_storage.py` | Schema DDL, canonical SQL constants (`INSERT_PENDING`, `UPDATE_RUNNING`, `UPDATE_COMPLETED`, `UPDATE_FAILED`, `SELECT_BY_ID`, `SELECT_LAST`), status constants, `now_iso()`, `new_id()`, `open_connection()` (WAL + autocommit + foreign_keys), `ensure_schema()`. |
| `_adapter.py` | Single `Adapter` class with three strategies (`passthrough`, `dataclass`, `pydantic`); duck-typed Pydantic detection. |
| `_types.py` | `Job` dataclass + `duration_ms` property; private `_parse_iso` helper. |
| `_registry.py` | Module-level `kind -> RegistryEntry`; `register()`, `lookup()`, `get_or_passthrough()`, `_reset()` (test hook). `auto_kind` flag wired through. |
| `_context.py` | `current_job` ContextVar + `JobContext`; `attach(key, value, flush=False)` raises `AttachOutsideJobError` outside a job and writes through via `recorder._flush_attach` when `flush=True`. |
| `_recorder.py` | `Recorder`: connection management, idempotent `shutdown`, sync context-manager protocol, all lifecycle SQL helpers (`_insert_pending`, `_mark_running`, `_mark_completed`, `_mark_failed`, `_row_status`, `_lookup_active_by_kind_key`, `_lookup_latest_failed`, `_flush_attach`), read methods (`get`, `last`), write-serialization via `_write_lock`. Module-level lazy default singleton + private `_set_default()` test hook. |
| `_decorator.py` | `@recorder` decorator (bare and factory forms); sync/async wrapper builders; bare-call lifecycle (insert pending → mark running → execute → terminal); cross-mode shims (`.sync`, `.async_run`); idempotency join (`_try_join_existing`, `_wait_for_join`, `_wait_for_terminal`); `.submit` stub raising `NotImplementedError` with phase-2 message. |
| `_errors.py` | `RecordedError` root + `ConfigurationError`, `AttachOutsideJobError`, `SyncInLoopError`. |

### Tests (`tests/`)

| file | scope |
|---|---|
| `conftest.py` | `db_path`, `recorder`, `default_recorder` (installs as module default), autouse registry-reset fixture. |
| `test_storage.py` | `now_iso` shape + sortability, `new_id` uniqueness/hex, schema bootstrap (columns, indexes, WAL), partial-unique-index behavior. |
| `test_types.py` | Adapter roundtrip (example + Hypothesis property tests, all three tiers); `Job.duration_ms` examples. |
| `test_attach.py` | Outside-job raises; intra-context buffering; `gather` task isolation; **`test_attach_buffers_until_completion_by_default`**; **`test_attach_flush_true_writes_through`**; **`test_async_attach_isolated_across_gather`**; projection+attach last-write-wins. |
| `test_read.py` | **`test_recorded_get_returns_rehydrated_typed_values`**, unknown id, unregistered kind passthrough, `last(n, kind="…")` glob filter, idempotent `Recorder.shutdown()`. |
| `test_lifecycle.py` | **`test_bare_call_writes_pending_running_terminal_in_order`** (async + sync); **`test_bare_call_returns_wrapped_functions_natural_value`** (async + sync); failure-path mark-failed; request-adapter capture; multi-arg envelope capture; unrecordable-response → mark-failed (anti-leak); `.submit` raises `NotImplementedError`. |
| `test_shims.py` | `.sync` runs async fn from sync caller; **`test_sync_call_inside_running_loop_raises_helpful_error`**; `.async_run` runs sync fn from async caller; contextvar/`attach()` propagation through `to_thread`. |
| `test_idempotency.py` | **`test_key_with_auto_kind_raises_at_decoration`** (sync + async); explicit-kind permits key; **`test_idempotency_collision_returns_existing_completed_without_reexecution`**; **`test_idempotency_collision_on_pending_waits_for_terminal`** (gather race + sync poll); **`test_idempotency_with_failed_retries_by_default`**; **`test_idempotency_retry_failed_false_returns_failed_job`**. |

### Project files

| file | role |
|---|---|
| `pyproject.toml` | hatchling-backed packaging; runtime deps = none; `[dev]` extras add pytest, pytest-asyncio (strict), hypothesis, pydantic. |
| `README.md` | Local-dev (`uv venv && uv pip install -e ".[dev]" && uv run pytest`); SQLite 3.35.0+ requirement. |
| `PROGRESS_1_1.md` | Sub-phase 1.1 progress note (existing). |
| `PROGRESS_PHASE1.md` | This report. |

## Named-test scoreboard

All 14 named tests are present and passing:

| named test | file | status |
|---|---|---|
| `test_bare_call_writes_pending_running_terminal_in_order` | `test_lifecycle.py` | PASS |
| `test_bare_call_returns_wrapped_functions_natural_value` | `test_lifecycle.py` | PASS |
| `test_async_attach_isolated_across_gather` | `test_attach.py` | PASS |
| `test_attach_buffers_until_completion_by_default` | `test_attach.py` | PASS |
| `test_attach_flush_true_writes_through` | `test_attach.py` | PASS |
| `test_idempotency_collision_returns_existing_completed_without_reexecution` | `test_idempotency.py` | PASS |
| `test_idempotency_collision_on_pending_waits_for_terminal` | `test_idempotency.py` | PASS (50/50 stress) |
| `test_idempotency_with_failed_retries_by_default` | `test_idempotency.py` | PASS |
| `test_idempotency_retry_failed_false_returns_failed_job` | `test_idempotency.py` | PASS |
| `test_key_with_auto_kind_raises_at_decoration` | `test_idempotency.py` | PASS |
| `test_sync_call_inside_running_loop_raises_helpful_error` | `test_shims.py` | PASS |
| `test_dataclass_roundtrip_via_data_slot` | `test_types.py` | PASS |
| `test_pydantic_roundtrip_when_installed` | `test_types.py` | PASS |
| `test_recorded_get_returns_rehydrated_typed_values` | `test_read.py` | PASS |

## Major design decisions made within DESIGN.md's guidance

1. **Two threading locks on `Recorder`.** `_lock` guards lazy connection init and `shutdown()`; `_write_lock` serializes all `execute()` calls so concurrent `asyncio.to_thread` writers don't race the connection's cursor state. Discovered empirically — without `_write_lock`, the gather-race idempotency test surfaced `sqlite3.InterfaceError: bad parameter or other API misuse`. Phase-2's worker should inherit this pattern or move to a connection-per-thread / single-writer-queue model.
2. **`_NO_JOIN` sentinel.** Internal sentinel returned by `_try_join_existing` to mean "no row found, proceed with INSERT" — distinct from `None` (which is a valid joined response). Private impl detail; pre-INSERT lookup is best-effort and both callers fall back to the partial-unique-index INSERT-then-catch path.
3. **`error=Model` is accepted in the decorator surface but not honored.** The decorator stores an `Adapter` for it on the registry, but `_serialize_error` always emits the natural `{type, message}` shape. Reserved for a future phase that wants to project structured errors. Left in the surface so users don't need to change call sites later.
4. **Idempotency joiner returns the response, not a `Job` — except for `retry_failed=False`.** Two return shapes from the same call surface depending on the join path: completed-or-pending join returns the rehydrated `response`; `retry_failed=False` against an existing failed row returns the `Job` itself (since there is no response to return). Matches DESIGN.md's prose intent ("return its result"); flagging because the asymmetry is real.
5. **Sync-poll wait at 5 ms cadence with a 30 s timeout.** Phase-2 will replace this with an in-process subscriber wakeup. The cadence is small enough that gathered tasks join promptly (~5–10 ms); the timeout is long enough that long broker-API jobs don't trip it during the bare-call lifecycle.
6. **`_capture_request` envelope rule.** Single positional arg + no kwargs → record `args[0]` (so `request=Model` validates the natural arg). Otherwise record `{"args": [...], "kwargs": {...}}`. Matches DESIGN.md's single-`req` examples without making multi-arg functions unrecordable.
7. **Hardened completion write.** If the wrapped function succeeds but `json.dumps(response)` raises (unrecordable type, no `response=` model), the wrapper marks the row `failed` with `recorder failed to serialize response: …` and re-raises the `TypeError`. Trades transparency for not leaking a `running` row given phase-1 has no reaper. The reaper in phase-2 makes this redundant; until then it preserves the audit invariant.
8. **`_serialize_recording_failure`** distinguishes function-side failures from recorder-side failures in the stored `error_json`, so debugging which side blew up doesn't require staring at exception types.

## DESIGN.md interpretations / clarifications

1. **"Decoration-time validation" of `key=` + auto-kind.** DESIGN.md prose says "decoration **raises at definition time**" but its own example shows the raise firing at `await place_order(req, key="order_42")`. Brief similarly says "decoration-time validation" while the named test is `test_key_with_auto_kind_raises_at_decoration`. I followed the example: at decoration the wrapper records `auto_kind=True`; at call time, if `key=` is passed against an auto-kind wrapper, raise `ConfigurationError` with the kind name and the explicit-kind fix. True definition-time would reject every `@recorder` plain-decorated function whether or not the user ever uses idempotency, which contradicts the bare-decorator pattern in DESIGN.md.
2. **`data=Model` projection from response.** DESIGN.md says "data is the typed projection over the response" with last-write-wins from `attach()`. Phase 1 ships best-effort projection: a dict response is filtered to declared field names for dataclass models, or fed through `model_validate` for Pydantic. Failure to project (missing required fields, wrong type) is silent — `data_json` falls back to the attach buffer alone. Test `test_attach_overrides_data_projection_last_write_wins` exercises the merge path.
3. **`retry_failed=False` on a *racing* pending row.** If the row I'm joining is `pending`/`running` when I look it up and *then* terminates as `failed`, the joining caller raises `RuntimeError(...)` rather than returning a Job. `retry_failed=False` is honored only when the failed row already exists at lookup time. DESIGN.md is silent here. Defensible — the joiner didn't observe failure as "prior history" — but the lead may want explicit semantics.
4. **`Recorder` `__enter__/__exit__` shipped.** Component 17's full "explicit Recorder context manager + atexit + configure" is out-of-scope, but the sync `with`-block is a one-method-pair convenience that the test fixture exercises (and that doesn't tie into the in-progress configure surface).
5. **Module-level default redirected via private `_set_default`.** The brief omits `configure()`; tests need the redirect. `_recorder._set_default()` is the test hook, which a future `configure()` would replace.

## Deviations from the brief

1. **Sub-phase boundary (1.4 lives in `_decorator.py`).** The brief enumerated 1.4 (idempotency + auto-kind validation) as a discrete sub-phase; I built it inside `_decorator.py` rather than a separate `_idempotency.py`. The logic is small (~80 lines) and tightly coupled to the wrapper's INSERT path, so a separate module felt like premature decomposition. If the lead prefers the split, it's a mechanical extract.
2. **Workflow's `PROGRESS.json`.** The brief mentions an optional `PROGRESS.json`; I used markdown progress reports instead (`PROGRESS_1_1.md` for the 1.1 stop-and-report and `PROGRESS_PHASE1.md` for this completion report). Better signal-to-noise for human review than a state file with `done: true` flags.
3. **Pydantic added to `[dev]` extras.** Required by `test_pydantic_roundtrip_when_installed` so it actually runs in CI rather than skip-by-default. Not a hard runtime dep — `recorded` itself never imports pydantic.

## Open questions / rough spots

1. **`retry_failed=False` against a *pending/running* row that subsequently fails** raises `RuntimeError` rather than returning a `Job`. (Item 3 under interpretations.) Confirm the desired semantics before phase 2.
2. **Joiner returns `response` vs `Job` asymmetry** when `retry_failed=False`. Same call surface, different return type depending on path. Matches DESIGN.md's prose but worth a deliberate decision before publishing.
3. **`error=Model` is accepted but inert.** Drop it from the surface, or wire it before publishing. (My pick: keep it inert + documented; users picking up the lib can opt in without API churn later.)
4. **Concurrency model under the worker (phase 2).** The single-connection + write-lock pattern works for phase-1's bare-call concurrency; phase-2's worker hammering the same connection from many tasks may need the connection-per-task or queue-serialized variant. Calling out so phase-2 doesn't inherit a hidden ceiling.
5. **`Recorder.shutdown()` and the default singleton.** A test that calls `Recorder.shutdown()` and then makes a module-level `recorded.get()` call would hit "Recorder is shut down" without the test explicitly clearing the default. The autouse `_clean_registry` fixture does NOT touch the default. Phase-2's `configure()` should formalize the lifecycle around this.
6. **`_capture_request` for multi-arg functions** stores `{"args": [...], "kwargs": {...}}`. If the user sets `request=Model` over a multi-arg function, the model has to accept this envelope shape — which is unlikely. Either reject multi-arg + `request=` at decoration or document. Today: silent.

## Test results

```
49 passed in 0.46 s
```

- Full suite under the 10-second budget (~22× margin).
- Each named test collected by exact name (verified via `pytest --collect-only`).
- Race-prone idempotency-pending test stress-tested 50× without flake.
- Smoke check: `python -c "from recorded import recorder, Recorder, attach, get, last, Job"` passes outside the test harness.

No commit yet — holding for lead review of the full diff.
