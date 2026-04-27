# Phase 2 Stream C — Completion report

Status: complete. **119 tests, 3.3 s** suite runtime on Python 3.13 /
SQLite 3.53. All 21 named tests across sub-phases 2.C.1 + 2.C.2
collected and passing. All 96 phase-1 + Stream A tests still pass.

Stream A's race-prone tests (`test_atomic_claim_under_concurrent_submit_load`,
the wait-primitive notify-vs-poll tests) still pass with no flakes
under the new wiring.

## Files

### Package (`src/recorded/`)

| file | role / change |
|---|---|
| `__init__.py` | Re-export `configure` and `attach_error`. Both added to `__all__`. |
| `_recorder.py` | New `join_timeout_s=` keyword on `Recorder.__init__` (default `DEFAULT_JOIN_TIMEOUT_S`); stored on `self.join_timeout_s`. New `__aenter__`/`__aexit__` (FastAPI lifespan). New module-level `configure(...)`: configure-once, only forwards explicitly-set kwargs, registers `atexit.register(r.shutdown)` exclusively on the configured default. New `_atexit_registered_for: set[int]` guard. New helper `_row_error_dict(job_id)` returning the raw decoded `error_json` dict (used by `JoinedSiblingFailedError.sibling_error`). New `_safe_deserialize(adapter, raw)` wrapper around `Adapter.deserialize` for the `error` slot in `_row_to_job` (rehydration must not crash when the recording fell back to the default `{type, message}` shape under `error=Model`). |
| `_handle.py` | `JobHandle.wait()` / `wait_sync()` read `self._recorder.join_timeout_s` when `timeout is None`. Dropped the `DEFAULT_JOIN_TIMEOUT_S` import. `_resolve_to_job` builds `sibling_error` from `_row_error_dict(...)` instead of inspecting `isinstance(job.error, dict)` (Stream-A interaction bug — see "Bugs found and fixed" §1). |
| `_decorator.py` | The two idempotency-join helpers (`_wait_for_terminal_sync`, `_async_wait_for_terminal`) now pass `recorder_inst.join_timeout_s` to their `_await_*` workhorses. `_validate_call_args(entry, key, args, kwargs)` extended with the multi-arg `request=Model` check. `_serialize_error` consults `JobContext.error_buffer` (set by `attach_error()`) and serializes through `entry.error` when present; falls back to `{type, message}` of the live exception on adapter failure. `_build_data_json` rewritten around new `_project_response(kind, model, response)` helper covering dict + dataclass-instance + pydantic-instance source shapes (the brief's §"data=Model projection generalization"). `_raise_for_failed_sibling` builds `sibling_error` from `_row_error_dict(...)` (same fix as `_handle.py`). |
| `_context.py` | New `attach_error(payload)` module-level function with `AttachOutsideJobError` precedent. New `JobContext.error_buffer` slot, default sentinel `_UNSET` (distinguishes "never set" from "set to None"). Last call wins (full-replace, not key-merged like `attach()`). |
| `fastapi.py` | **new** — single `async def capture_request(request)` returning `{method, path, query, headers (case-folded), body (utf-8 or None)}`. Stdlib-only: no `starlette` / `fastapi` import; duck-types on `method`, `url`, `headers`, `query_params`, `body`. Raises `TypeError` for non-Request-shaped objects. Module docstring documents the duck-typing rationale. |

### Tests (`tests/`)

| file | scope |
|---|---|
| `test_configure.py` | **new** — all 7 named tests for 2.C.1. Local autouse fixture clears `_default` before/after each test so `configure-once` semantics don't leak across tests. The atexit test uses `subprocess.run([sys.executable, "-c", ...])` capped at 5 s and asserts no stale `-wal`/`-shm` files remain after exit. |
| `test_attach_error.py` | **new** — 6 tests covering `attach_error()` contract, `error=Model` wiring, full-replace semantics, outside-job raise, fallback paths (no `attach_error()` call, adapter-validation failure), and end-to-end through the worker loop. |
| `test_data_projection.py` | **new** — 3 tests covering `data=Model` projection over dataclass response, pydantic response instance, and the existing dict-source path (regression). Uses `pytest.importorskip("pydantic")` at module top — already available via dev extras. |
| `test_request_model_check.py` | **new** — 3 tests: multi-arg + `request=Model` raises, kwargs + `request=Model` raises, single-positional still works (regression). |
| `test_fastapi.py` | **new** — 4 tests using FastAPI's `TestClient`. `pytest.importorskip("fastapi")` at module top; full suite still runs without FastAPI installed. |

### Project files

| file | role |
|---|---|
| `pyproject.toml` | Added `fastapi>=0.110` to `[dev]` extras. Core requires-python and runtime `dependencies = []` unchanged. |
| `PROGRESS_2_C_1.md` | Mid-point report after sub-phase 2.C.1 (already shipped before sub-phase 2.C.2 work began). |
| `PROGRESS_PHASE2_C.md` | This report. |

## Named-test scoreboard

All 21 named tests across both sub-phases present and passing.

### Sub-phase 2.C.1

| named test | file | status |
|---|---|---|
| `test_configure_returns_recorder_with_given_path` | `test_configure.py` | PASS |
| `test_configure_is_no_op_after_first_call` | `test_configure.py` | PASS |
| `test_configure_sets_join_timeout_seen_by_handle_wait_default` | `test_configure.py` | PASS |
| `test_configure_join_timeout_does_not_override_explicit_per_call_timeout` | `test_configure.py` | PASS |
| `test_configure_join_timeout_reaches_idempotency_join_helpers` | `test_configure.py` | PASS |
| `test_atexit_runs_shutdown_on_configured_default_only` | `test_configure.py` | PASS |
| `test_recorder_async_context_manager_bootstraps_and_shuts_down` | `test_configure.py` | PASS |

### Sub-phase 2.C.2

| named test | file | status |
|---|---|---|
| `test_attach_error_populates_error_slot_through_model_adapter` | `test_attach_error.py` | PASS |
| `test_attach_error_full_replace_semantics_last_call_wins` | `test_attach_error.py` | PASS |
| `test_attach_error_outside_job_raises` | `test_attach_error.py` | PASS |
| `test_error_model_falls_back_to_type_message_when_attach_error_not_called` | `test_attach_error.py` | PASS |
| `test_error_model_serialization_failure_falls_back_to_type_message` | `test_attach_error.py` | PASS |
| `test_attach_error_works_through_worker_loop` | `test_attach_error.py` | PASS |
| `test_data_projection_dataclass_response` | `test_data_projection.py` | PASS |
| `test_data_projection_pydantic_response_instance` | `test_data_projection.py` | PASS |
| `test_data_projection_pydantic_response_dict_still_works` | `test_data_projection.py` | PASS |
| `test_request_model_with_multi_positional_args_raises_configuration_error` | `test_request_model_check.py` | PASS |
| `test_request_model_with_kwargs_raises_configuration_error` | `test_request_model_check.py` | PASS |
| `test_fastapi_capture_request_serializes_envelope` | `test_fastapi.py` | PASS |
| `test_fastapi_capture_request_empty_body_is_none` | `test_fastapi.py` | PASS |
| `test_fastapi_capture_request_round_trips_through_request_slot` | `test_fastapi.py` | PASS |

## Major design decisions made within DESIGN.md's guidance

1. **`configure()` is configure-once via "first wins, ignore later
   kwargs."** A second `configure(...)` returns the existing default
   silently — no warning, no raise, no field swap. Matches the brief's
   resolved-decision row exactly. The docstring spells out that tests
   and explicit-instance code paths must use `Recorder(...)` directly
   to get a different shape.

2. **`configure()` only forwards the kwargs the caller actually set.**
   `None`-valued args are dropped from the kwargs dict, so any future
   `Recorder.__init__` default change (e.g. bumping the reaper
   threshold) is picked up by `configure()` callers automatically;
   we never freeze the current defaults into `configure()`'s call
   chain.

3. **`atexit` registered only on the configured default — direct
   `Recorder(...)` doesn't register.** The brief's guidance ("Only the
   *configured* default registers atexit. Document.") is honored at
   `configure()` (registers) and `_set_default()` (does not). Doc
   strings on both call out the asymmetry. Test fixtures bulk-construct
   recorders via `_set_default(r)` and would otherwise accumulate
   hundreds of atexit handlers per session.

4. **`_atexit_registered_for: set[int]` guard.** Configure-once already
   guarantees we never register twice for the same default, but the
   guard makes the property local to the registration site rather
   than relying on configure-once invariants holding forever. Cheap
   belt-and-suspenders.

5. **`__aenter__` / `__aexit__` use `asyncio.to_thread`.** `_connection()`
   (with the reaper sweep) and `shutdown()` are blocking; running them
   on the loop thread under FastAPI lifespan would block any
   concurrently-starting task. Matches every other "SQLite work from
   an async context" callsite in the package.

6. **`JobHandle.wait(timeout=None)` reads `recorder.join_timeout_s`
   lazily on each call, not at handle construction.** A handle
   constructed before `configure()` (unlikely but possible in test
   paths that swap the default) still picks up the configured timeout
   the first time `.wait(timeout=None)` runs.

7. **`attach_error()` writes to a single slot with full-replace
   semantics** (vs. `attach()`'s key-merge into a dict buffer). The
   sentinel `_UNSET` distinguishes "user never called `attach_error()`"
   from "user called `attach_error(None)`" (the latter is a deliberate
   choice to record `null`).

8. **`_serialize_error` consults `current_job.get()` from the worker
   path correctly.** Verified explicitly: in `_worker.py::_execute`,
   `_serialize_error(entry, exc)` is evaluated as an argument to
   `asyncio.to_thread(self.recorder._mark_failed, ..., _serialize_error(entry, exc))`.
   Argument evaluation runs on the loop thread under the task's context
   (where `current_job.set(ctx)` happened). The test
   `test_attach_error_works_through_worker_loop` exercises this path
   end-to-end.

9. **`_build_data_json` factored into `_project_response(kind, model, response)`
   helper.** Six source shapes (3 per `_kind`); a single inline
   if/elif tree got unwieldy. The helper raises only on hard bugs
   (caught and folded to empty-projection by the caller), matching the
   "projection is opportunistic" contract.

10. **Pydantic-other-model projection uses
    `model_fields` for filter names**, dataclass-other-instance
    projection uses `dataclasses.fields(model)`. Both are stable v2
    APIs; we don't import pydantic to discover them (we rely on the
    user-supplied class carrying the v2 protocol, same precedent as
    `_adapter.py::_is_pydantic_model`).

11. **FastAPI helper is duck-typed via `_REQUIRED_ATTRS = ("method",
    "url", "headers", "query_params")`.** `body` is awaited
    unconditionally — every Starlette-shaped Request exposes
    `await request.body()`, including FastAPI/`Request` and bare
    Starlette. Header keys case-folded so downstream queries don't
    fragment on the same logical header.

12. **Multi-arg `request=Model` check lives inside `_validate_call_args`,
    not a parallel callsite.** The brief said "extend, don't add a
    parallel check site." `_validate_call_args` is now called from
    both wrappers and `.submit` — all three pick up the new branch.

## DESIGN.md interpretations / clarifications

1. **Reaper still runs on connection bootstrap, not on `__aenter__`.**
   `__aenter__` calls `_connection()`, which is the existing reaper
   trigger (Stream A's design). FastAPI lifespan thus sweeps stuck
   rows on app startup without a separate reaper-on-aenter path.

2. **`configure(path=None)` falls back to the `Recorder.__init__`
   default (`./jobs.db`).** DESIGN.md's "DB path resolution: explicit
   `Recorder(path=...)` → `recorded.configure(path=...)` → `./jobs.db`"
   is preserved by the only-forward-set-keywords approach: passing no
   path lets `Recorder.__init__` pick its default.

3. **`Job.error` rehydration with `error=Model` falls back to raw dict
   on adapter failure** (new `_safe_deserialize` helper in `_recorder.py`).
   Without this, `recorder.get(job_id)` / `last(...)` / `list(...)`
   would crash on any row whose `error_json` was recorded under the
   default `{type, message}` fallback (i.e. the wrapped function never
   called `attach_error()`). DESIGN.md's read-API contract says the
   value is rehydrated *if* a model was registered — silent rehydration
   crash isn't intended. Affects only the `error` slot; `request`,
   `response`, `data` rehydration semantics are unchanged. Documented
   inline in `_row_to_job`.

4. **`JoinedSiblingFailedError.sibling_error` is the raw dict,
   regardless of whether `error=Model` is registered.** Pre-existing
   code (`_handle.py::_resolve_to_job` + `_decorator.py::_raise_for_failed_sibling`)
   built `sibling_error` from `isinstance(job.error, dict)`, which
   silently produced `None` whenever rehydration converted the field
   to a model instance. Both call sites now use the new
   `Recorder._row_error_dict(...)` helper. The contract surfaced by
   the existing `test_handle.py::test_submit_idempotency_collision_with_retry_failed_false_raises_on_wait`
   (which expects `{type, message}` for the unmodeled case) still
   holds — the dict shape is identical regardless of `error=Model`.

5. **`attach_error()` payload of `None`** (i.e. `attach_error(None)`)
   is treated as "user wants to record `null`", distinct from "user
   never called `attach_error()`." Sentinel `_UNSET` enforces this.
   Brief is silent; this is the safer of the two interpretations
   (preserves intent if the user deliberately wrote `None`).

## Deviations from the brief

1. **Added `_safe_deserialize(adapter, raw)` for the `error` slot in
   `_row_to_job`.** Not in the brief's surface list. Without it, the
   read API crashes for any error-modeled row whose recording fell
   back to `{type, message}`. See "Interpretations" §3 for the
   rationale.

2. **Added `Recorder._row_error_dict(job_id)` helper + rewired both
   `JoinedSiblingFailedError.sibling_error` callsites.** Not in the
   brief. This is a Stream-A interaction bug fixed inline (see
   "Bugs found and fixed" §1).

3. **`test_data_projection_*` tests pass `response=` alongside `data=`
   so the response slot can `json.dumps` the dataclass / pydantic
   instance.** Without `response=Model`, the passthrough adapter
   returns the model instance unchanged and `json.dumps` raises. The
   brief's named tests don't mention this — but it's the realistic
   user shape ("if you return a typed model, you also need to declare
   `response=` so the response_json serializer round-trips through
   `model_dump`/`asdict`"). The dict-source projection regression test
   keeps `response=` unset because dicts are JSON-serializable
   directly. See "Open questions" §3 for the underlying tension.

4. **`fastapi.py` exports a single `capture_request`, not a class with
   methods.** Brief used the name `capture_request(request)` and the
   signature is the one-function shape; nothing here actually deviates
   — flagged for completeness.

5. **`__init__.py` ordering of `attach`/`attach_error`** — both are
   exported from `_context`, and `attach_error` is listed
   alphabetically in `__all__` (right after `attach`). Trivial.

## Bugs found and fixed

1. **`JoinedSiblingFailedError.sibling_error` collapsed to `None`
   under `error=Model`** (Stream-A interaction bug). Both
   `_handle.py::_resolve_to_job` and
   `_decorator.py::_raise_for_failed_sibling` built `sibling_error`
   via `isinstance(job.error, dict)`. With `error=Model` registered,
   `Recorder.get(...)` rehydrates `Job.error` as the model instance,
   which is not a dict, so `sibling_error` collapsed to `None`. The
   surfaced symptom: `test_attach_error_works_through_worker_loop`
   would assert against the model's payload but get `None` back from
   the exception. Fixed by adding `Recorder._row_error_dict(job_id)`
   which fetches `error_json` and decodes it directly — bypassing the
   model rehydration. The unmodeled-error contract surfaced by
   `test_handle.py::test_submit_idempotency_collision_with_retry_failed_false_raises_on_wait`
   ("`sibling_error == {type, message}`") still passes — the raw dict
   shape is identical to what `Job.error` was producing under the
   passthrough adapter.

2. **Read-API rehydration crashed on default-shape `error_json` under
   `error=Model`.** The fallback path (`attach_error()` not called →
   `{type, message}` recorded) couldn't be rehydrated as
   `BrokerError(**{"type":..., "message":...})`. `recorder.get(job_id)`
   would raise `TypeError`. Fixed by `_safe_deserialize` falling back
   to the raw dict when adapter rehydration raises. Affects only the
   `error` slot (the others can also fail in principle, but the
   `error` slot is where the asymmetric "fallback-shape vs. model"
   tension lives — the others always recorded under the model when
   set).

## Open questions / rough spots

1. **`configure()` returns the existing default silently on a second
   call.** A loud-on-second-call variant (`ConfigurationError` if
   `_default is not None`) would catch app-startup misconfiguration
   bugs (two competing `configure()` calls in different module-init
   paths). The brief's resolved-decision row says "no warning, no
   raise" so I went with silent. Easy to flip later if the lead wants
   a stricter contract.

2. **`configure()` does NOT take a `recorder=` instance argument.** A
   caller who wants to install a pre-built `Recorder` as the default
   still has to use `_set_default(r)` (private). Stream C's ~150-LOC
   budget isn't tight on this, but neither DESIGN.md nor the brief
   asks for the public surface; flag in case the lead wants a public
   `install(r)` later.

3. **Tension between `data=Model` projection and "`response` is full
   by default."** When the wrapped function returns a typed model
   instance (e.g. `BrokerOrder`) and the user only declares
   `data=OrderView`, the recorder needs to:
   (a) project the response into `data_json` (Stream C now does this
   for dataclass + pydantic instances), AND
   (b) serialize the response into `response_json` via the passthrough
   adapter — which is `json.dumps(model_instance)`, which crashes.
   The user's options today: (i) declare `response=BrokerOrder` (round
   trips through the model — works, but loses any "extra" fields the
   server might have included that aren't in the model), (ii) return
   a dict instead of a model, (iii) wait for a future "passthrough
   adapter for dataclass/pydantic instances" extension to detect-and-
   dump. The data projection tests in this stream go with option (i)
   to keep them realistic. A `response=Model.model_dump_only` mode
   would match DESIGN.md's "audit invariant" — out of scope here.
   Surfacing for the lead.

4. **`attach_error` payload sentinel `_UNSET` is module-private to
   `_context.py` but referenced from `_decorator.py`.** Imported as
   `from ._context import _UNSET, JobContext, current_job`. Exposing
   it from `_context.py` keeps `_serialize_error` decoupled from a
   second source of truth. Could be hidden behind a method on
   `JobContext` (`ctx.has_attached_error()`) for cleaner encapsulation;
   I went with the sentinel exposure to match the dataclass-field
   precedent already used in the codebase.

5. **The FastAPI test helper imports `fastapi.testclient.TestClient`,
   which itself depends on `httpx` (already in dev extras).** `httpx`
   was added in phase-1 integration tests for `pytest-httpserver`, so
   no new transitive surface. Just flagging.

6. **`_serialize_error`'s contextvar visibility from the worker
   loop's `_execute` task.** `asyncio.to_thread(self.recorder._mark_failed,
   job_id, _storage.now_iso(), _serialize_error(entry, exc))` evaluates
   `_serialize_error(entry, exc)` on the loop thread *under the task's
   context* (where `current_job.set(ctx)` happened). The contextvar
   `current_job.get()` therefore sees the worker's `JobContext`, and
   `attach_error()` payloads set inside the wrapped function reach
   the serializer. End-to-end exercised by
   `test_attach_error_works_through_worker_loop`.

7. **Suite runtime: 3.31 s** vs. Stream A's 2.5 s baseline. The
   delta comes mostly from the FastAPI tests (full TestClient
   roundtrip imports + asgi life cycle ≈ 0.5 s) and the atexit
   subprocess (≈ 0.05 s). Well inside the 12 s phase-2 budget.

## Test results

```
119 passed in 3.31s
```

- 96 phase-1 + Stream A tests still pass.
- 23 new tests across 5 new files for sub-phases 2.C.1 + 2.C.2.
- All 21 named tests collected and passing.
- Smoke check: `python -c "from recorded import recorder, Recorder, JobHandle, attach, attach_error, configure, get, last, list, connection"` passes.
- Full suite under the 12 s phase-2 budget.

No commit — diff is clean and held for lead review per the brief.
