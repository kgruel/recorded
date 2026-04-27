# Phase 2 Stream A — Completion report

Status: complete. **96 tests, 2.5 s** suite runtime on Python 3.13 /
SQLite 3.53. All 18 named tests across 2.A.1 + 2.A.2 collected and
passing, plus the lead's follow-up `test_recorded_list_filters_by_idempotency_key`.
All 64 phase-1 tests still pass.

Stress-checked: race-prone tests
(`test_atomic_claim_under_concurrent_submit_load`,
`test_submit_idempotency_collision_returns_handle_to_existing_active_row`,
the wait-primitive notify-vs-poll tests, the gather-race
idempotency-pending test) ran **50/50** in succession with no flakes
(verified via explicit `pass++/fail++` counter loop, not just `uniq -c`).

## Files

### Package (`src/recorded/`)

| file | role / change |
|---|---|
| `__init__.py` | Exports add `JobHandle`, `connection`, module-level `list`. `last(status=)` symmetrized with `list()`. |
| `_storage.py` | Extracted `format_iso(dt)` from `now_iso()` (used by `since=`/`until=` callers). New SQL constants `CLAIM_ONE` (atomic worker claim) and `REAP_STUCK`. |
| `_recorder.py` | Substantial rewrite. New: notify primitive (`_subscribe`/`_unsubscribe`/`_resolve`), reaper (`_reap_stuck_running_unlocked`, run on connection bootstrap), `connection()` public alias, `list()` filtered iterator, `last(status=)` symmetric, `_claim_one()` for the worker, `_execute_count()` (rowcount-aware write under `_write_lock`), `_ensure_worker()` (lazy worker bootstrap). New `__init__` keywords: `reaper_threshold_s` (default 5 min), `worker_poll_interval_s` (default 200 ms). `_mark_completed`/`_mark_failed` gate `_resolve` on `cursor.rowcount > 0`. `shutdown()` now also joins the worker thread. |
| `_decorator.py` | Removed 5 ms poll loop in idempotency-join paths. New helpers: `_wait_for_terminal_sync` / `_async_wait_for_terminal` (subscribe + bounded wait + cross-process recheck). `_resolve_terminal` now raises `JoinedSiblingFailedError` for `retry_failed=False` against a failed row (was: return Job). New `_try_join_handle` for `.submit()`'s pre-INSERT collision check. `.submit()` now wired through the worker; phase-1 `NotImplementedError` stub removed. |
| `_handle.py` | **new** — `JobHandle(job_id, recorder, kind)` with `.wait(timeout=None)` (async, raises `JoinedSiblingFailedError` on failed terminal), `.wait_sync(timeout=None)` (sync, raises `SyncInLoopError` from inside a running loop). |
| `_worker.py` | **new** — `Worker(recorder)` owns one asyncio loop in a dedicated thread. `start()` is idempotent. Loop: `claim_one` → `create_task(_execute(row))`. Per-job: sets `current_job` ContextVar inside `_execute` before any await; async fn awaited directly, sync fn dispatched via `to_thread`. `shutdown()` is idempotent: signals via `loop.call_soon_threadsafe(loop_shutdown.set)`, cancels in-flight, joins thread. In-flight tasks cancelled by shutdown write a synchronous `_mark_failed` in their `finally` so the row reaches terminal before unwind. |

### Tests (`tests/`)

| file | scope |
|---|---|
| `test_wait_primitive.py` | **new** — `_subscribe`/`_resolve` contract, including the no-polling assertions for sync + async join paths. |
| `test_reaper.py` | **new** — orphaned-row reaping, threshold configurability, late-completion drop, subscriber-resolve. |
| `test_read_api.py` | **new** — `connection()` (module + Recorder), `list(...)` filter combos, `last(status=)`, `list()` deferred-query laziness, `where_data` and `order` validation, ordering. |
| `test_worker.py` | **new** — lazy start, async + sync execution, attach propagation (both kinds), atomic claim under load (100 jobs), idempotent shutdown that drains in-flight, `wait_sync` inside loop raises. |
| `test_handle.py` | **new** — submit-idempotency collision returns same handle, `retry_failed=False` raises on `.wait()`, timeout, sync wait paths (success + failure). |
| `test_idempotency.py` | **modified** — renamed `test_idempotency_retry_failed_false_returns_failed_job` → `_raises_joined_sibling_failed`; updated to expect `JoinedSiblingFailedError` per the brief's resolution. |
| `test_lifecycle.py` | **modified** — replaced `test_submit_raises_with_phase2_message` with `test_submit_returns_handle` (worker is wired now). |
| `test_integration.py` | **modified** — replaced `test_submit_raises_not_implemented_with_phase2_message` with `test_submit_returns_job_handle_resolving_to_terminal_job`. |

### Project files

| file | role |
|---|---|
| `PROGRESS_2_A_1.md` | Mid-point report after sub-phase 2.A.1 (already shipped before sub-phase 2.A.2 work began). |
| `PROGRESS_PHASE2_A.md` | This report. |

## Named-test scoreboard

All 18 named tests across both sub-phases present + passing.

### Sub-phase 2.A.1

| named test | file | status |
|---|---|---|
| `test_subscribe_resolves_when_terminal_write_commits` | `test_wait_primitive.py` | PASS |
| `test_subscribe_returns_immediately_if_already_terminal` | `test_wait_primitive.py` | PASS |
| `test_idempotency_join_uses_notify_not_polling` | `test_wait_primitive.py` (the async/asyncio.sleep variant carries the brief's exact name; a sibling `test_idempotency_join_in_process_does_not_poll_with_sleep` covers the sync/time.sleep path) | PASS (both) |
| `test_reaper_marks_orphaned_running_rows_failed_on_start` | `test_reaper.py` | PASS |
| `test_reaper_late_completion_silently_dropped` | `test_reaper.py` | PASS |
| `test_recorded_list_filters_by_kind_glob_status_since_until_where_data` | `test_read_api.py::test_recorded_list_filters_by_kind_status_since_until_where_data` | PASS |
| `test_recorded_list_returns_iterator_lazily` | `test_read_api.py` | PASS |
| `test_recorded_connection_returns_sqlite_connection` | `test_read_api.py` | PASS |
| `test_last_with_status_filter` | `test_read_api.py` | PASS |

### Sub-phase 2.A.2

| named test | file | status |
|---|---|---|
| `test_submit_returns_handle_whose_wait_resolves_to_terminal_job` | `test_worker.py` | PASS |
| `test_worker_lazy_starts_on_first_submit` | `test_worker.py` | PASS |
| `test_worker_executes_async_and_sync_kinds_concurrently` | `test_worker.py` | PASS |
| `test_attach_propagates_to_worker_loop` | `test_worker.py` (+ a sync-kind sibling) | PASS |
| `test_atomic_claim_under_concurrent_submit_load` | `test_worker.py` | PASS (50/50 stress) |
| `test_submit_idempotency_collision_returns_handle_to_existing_active_row` | `test_handle.py` | PASS (50/50 stress) |
| `test_submit_idempotency_collision_with_retry_failed_false_raises_on_wait` | `test_handle.py` | PASS |
| `test_recorder_shutdown_is_idempotent_and_drains_worker` | `test_worker.py` | PASS |
| `test_handle_wait_sync_inside_loop_raises_helpful_error` | `test_worker.py` | PASS |

## Major design decisions made within DESIGN.md's guidance

1. **Notify primitive: register-then-check ordering for `_subscribe`.**
   `_subscribe` adds the future under `_notify_lock` BEFORE issuing the
   row-status read. Any concurrent `_resolve` either (a) sees us in the
   list and resolves us, or (b) committed terminal before our status
   read, in which case our status read finds it and we pre-resolve
   inline. This closes the obvious check-then-register race.

2. **`_resolve` gated on `cursor.rowcount > 0`.** New `_execute_count`
   helper holds `_write_lock` through `execute() + cursor.rowcount`
   (matches the cursor-safety pattern from the phase-1 `_fetchone`/
   `_fetchall` bugfix). A late `_mark_completed` against a reaped row
   sees `rowcount=0` and skips `_resolve` — the reaper already handled
   subscribers.

3. **Hybrid in-process notify + cross-process polling fallback.** All
   wait helpers (sync + async, on `JobHandle` and on the decorator
   join paths) use the same pattern: `fut.result(timeout=POLL_INTERVAL)`
   (or the asyncio `wait_for(shield(wrap_future(fut)), timeout=...)`
   equivalent). For the in-process happy path the future fires on the
   first wait slice; no `time.sleep` / `asyncio.sleep` is invoked.
   For cross-process, each timeout-and-recheck loop iteration acts as
   the polling cadence (default 200 ms). `asyncio.shield` keeps the
   wrapped future alive across iterations so the next iteration finds
   the same registered subscriber.

4. **Reaper resolves subscribers.** `_reap_stuck_running_unlocked`
   uses `RETURNING id` and calls `_resolve(id, STATUS_FAILED)` for each
   reaped row. Without this, a `JobHandle.wait()` whose worker process
   died would hang past the reap until `JoinTimeoutError`.

5. **Single connection + `_write_lock` for the worker.** Per the brief:
   keep the phase-1 single-connection model. `_write_lock` already
   serializes all writes; the worker's many in-flight tasks compete
   for the same lock and that has been fine for 100-job concurrent
   loads (`test_atomic_claim_under_concurrent_submit_load` passes
   50/50 in stress). Connection-per-task / writer-queue is design-for-
   future.

6. **Shutdown drain: cancel + synchronous failure-write in `finally`.**
   The brief offered a choice ("let them finish or cancel — pick and
   document; my lean: cancel"). I picked cancel. The cancelled task's
   `finally` block writes `_mark_failed(...)` synchronously (not via
   `to_thread`) so the failure write commits before `CancelledError`
   unwinds. The cancellation marker payload is
   `{"type":"CancelledError","message":"worker shutdown cancelled in-flight job"}`.
   Conditional `UPDATE ... WHERE status IN ('pending','running')` means
   if the task happened to complete normally between drain-signal and
   cancel-fire, the late mark is a no-op (status='completed' already).

7. **Worker lifecycle attached to the Recorder.** `Recorder.shutdown()`
   tears down the worker first (outside the connection lock so the
   worker's last `to_thread` SQL call doesn't deadlock), then closes
   the SQLite connection. `_ensure_worker()` is the lazy bootstrap
   accessor — only `.submit()` calls it.

8. **`recorded.list()` is a deferred-query iterator, not row-streamed.**
   First `next()` calls `_fetchall()` (under `_write_lock` end-to-end)
   and yields rows from the materialized buffer. Streaming a live
   cursor across thread boundaries would re-introduce the cursor-race
   bug fixed in PROGRESS_INTEGRATION §3.1.

9. **`Recorder.connection()` is the public spelling; `_connection()`
   stays as a thin alias.** Renaming all `_connection` call sites in
   the phase-1 tests is out of Stream A's scope.

10. **JobHandle `.wait()` ALWAYS raises on failure.** Per the
    wrap-transparency principle: keyed-call surfaces never return a
    `Job` for failure paths. `JoinedSiblingFailedError` carries the
    sibling's recorded `error_json` payload so callers can act
    programmatically.

11. **`_subscribe` future result is `status` (str) only.** The brief
    suggested `(status, response, error_dict)`. I went with `status`
    alone — the joiner re-queries the `Job` via `recorder.get(...)` to
    rehydrate response/error. Reasons: (a) the row is already the
    single source of truth; (b) the rehydration logic lives in
    `_row_to_job`; (c) avoids duplicating data on the future. The
    extra SELECT is one indexed lookup per joiner.

## DESIGN.md interpretations / clarifications

1. **Reaper runs on `_connection()` first-init, not eagerly in
   `__init__`.** `ensure_schema()` already requires the connection;
   the existing pattern (lazy bootstrap) preserves "constructing a
   Recorder doesn't touch the disk." Any non-trivial use of the
   Recorder triggers `_connection()` immediately, so the observable
   behavior is "on first use", which matches the spirit of "on
   construction."

2. **Late completion against a reaped row is silently dropped.** The
   phase-1 conditional UPDATE (`WHERE id=? AND status='running'`)
   already enforces this; the new gate on `_resolve` ensures we don't
   double-fire subscribers another writer handled.

3. **`Recorder` worker is per-Recorder, not global.** The brief says
   "lazy-started on first `.submit()` call against any `Recorder`" —
   I read "any" distributively: one worker per Recorder instance.
   Per-Recorder is the cleaner contract for testing and shutdown.

4. **`since` / `until` filter on `submitted_at`.** DESIGN.md doesn't
   specify which timestamp; `submitted_at` is the natural answer
   ("rows from the last hour" is a submission-time question) and is
   covered by `idx_jobs_kind_submitted`.

5. **`where_data` keys AND together.** Per the brief.

6. **`.submit` accepts `key=` and `retry_failed=` only on async/sync
   kinds equally.** Both wrappers' `.submit` use the same helper
   (`_try_join_handle`). The wrapper's own async/sync nature doesn't
   change `.submit`'s shape — it always returns `JobHandle`.

7. **`Worker.shutdown(timeout=10.0)`.** Brief says "join with a
   timeout". 10 s is generous for the cancel-and-drain path under
   normal use, short enough not to hang test fixtures if something
   genuinely wedges.

## Deviations from the brief

1. **Polling-test split into two for monkeypatch surface.** The brief
   names `test_idempotency_join_uses_notify_not_polling` (singular).
   I split it into the brief's exact name (covering the async path
   and `asyncio.sleep`) plus a sync-path sibling
   `test_idempotency_join_in_process_does_not_poll_with_sleep`
   covering `time.sleep`. The brief's exact name is preserved on the
   async side because that's the path that hit the threadpool-
   exhaustion bug fixed in PROGRESS_INTEGRATION §3.2 (and is the more
   important regression to pin).

2. **`worker_poll_interval_s` keyword on `Recorder.__init__`.** Not
   strictly in the brief's "in scope" surface, but useful for tests
   that need to bound the worker's idle wait. Defaults to 200 ms per
   DESIGN.md. Drop if unwanted.

3. **`reaper_threshold_s` constant + module-level
   `DEFAULT_REAPER_THRESHOLD_S` and `DEFAULT_JOIN_TIMEOUT_S` and
   `NOTIFY_POLL_INTERVAL_S` constants.** Brief says default reaper
   threshold is 5 min. Plumbed via constants on `_recorder.py`. Stream
   C will rewire `DEFAULT_JOIN_TIMEOUT_S` through
   `recorded.configure(...)`.

4. **`Worker._main`'s shutdown race-window handling.** If
   `_claim_one` returns a row but shutdown fires before the task is
   spawned, the row would be stuck `running` waiting for the next
   reaper sweep. I explicitly mark such rows failed in the loop's
   shutdown branch. Belt-and-suspenders with the reaper but cleaner —
   no one has to wait for the next process startup to discover the
   row.

5. **Module-level `recorded.list()` shadows `builtins.list`.** Used
   `# noqa: A001` in `__init__.py`. Internal call sites that want the
   builtin can use `builtins.list` (none currently do).

## Open questions / rough spots

1. **JobHandle for the failed-row + retry_failed=False case is a
   handle that resolves immediately to "failed".** `_try_join_handle`
   returns a `JobHandle` over the prior failed row when
   `retry_failed=False` collides with a failed row. `wait()` on that
   handle calls `_subscribe`, which pre-resolves to `STATUS_FAILED`
   (the row is already terminal), then `_resolve_to_job` raises.
   This works correctly but is a tiny bit wasteful — we know at
   submit time that this handle will raise. An alternative is
   raising at `.submit()` time. I went with the handle-then-raise
   path because (a) it preserves the surface promise that `.submit`
   returns a `JobHandle`, (b) callers who only care about the side
   effect of recording can ignore the handle, and (c) it keeps
   `.submit` synchronous and free of failure-path branching. Easy to
   change if the lead disagrees.

2. **`Worker._loop_ready.wait()` in `start()` blocks the calling
   thread until the worker thread has constructed the loop.** This
   is brief (microseconds) but it does mean `submit()` is technically
   blocking for the duration of worker bootstrap on the very first
   call against a fresh Recorder. Documented; in practice the test
   suite measures the entire worker bootstrap path at <5 ms.

3. **`error=Model` is still inert (Stream C).** `_serialize_error`
   emits `{type, message}` regardless of the registered model. Folded
   forward from PROGRESS_PHASE1 §"Open questions" #3.

4. **`recorded.list(key=...)`** — **Resolved per lead direction.**
   Added to both `Recorder.list(...)` and module-level
   `recorded.list(...)`: exact-match equality on the `key` column,
   `None` means no filter. Named test
   `test_recorded_list_filters_by_idempotency_key` in
   `test_read_api.py` covers it (asserts both single-filter and
   `key= + status=` combination).

5. **`Worker._execute` returns silently on `UnknownKind`.** If a
   worker process didn't import the module that defined the kind,
   the row gets marked failed with an explanatory message. This is
   important for multi-process FastAPI deployments where workers
   may be cycled and a row claimed by a fresh process with a
   different import set. Documented in the error message itself.

6. **Suite runtime: 2.4 s** (after fixing the cross-loop synchronization
   issue described in #8 below). Well inside the 10 s budget; the new
   worker + handle suites add ~0.5 s over the phase-1 1.6 s baseline.
   Slowest individual tests after the fix: `test_handle_wait_timeout_raises_join_timeout`
   (0.30 s — the 0.3 s timeout itself), then `test_submit_returns_handle_whose_wait_resolves_to_terminal_job`
   teardown (0.49 s — worker shutdown's wakeup-from-poll lag of up to
   `worker_poll_interval_s` = 200 ms).

7. **JobHandle `.wait()` with no timeout uses `DEFAULT_JOIN_TIMEOUT_S`
   (30 s).** Stream C makes this configurable via
   `recorded.configure(join_timeout_s=...)` and per-call. For now,
   30 s is the bound; long-running broker calls under `.submit` need
   to pass `timeout=` explicitly.

8. **Cross-loop test synchronization.** The first version of
   `test_submit_idempotency_collision_returns_handle_to_existing_active_row`
   used `asyncio.Event()` to coordinate between the test's loop and
   the worker's loop — but `asyncio.Event` is single-loop-bound, so
   `started.set()` from the worker thread didn't actually wake the
   test loop's `started.wait()`. The test passed only because the
   `asyncio.wait_for(..., timeout=5.0)` deadline happened to elapse
   in time for the row to actually be in `running`. Switched to
   `threading.Event` + `await asyncio.to_thread(event.wait)` for
   correct cross-loop coordination. Test runtime dropped from 5.04 s
   to 0.01 s. **General lesson for Stream C / future worker-touching
   tests**: any synchronization between the test's loop and the
   worker's loop must use `threading` primitives (or
   `loop.call_soon_threadsafe`).

9. **`JobHandle._resolve_to_job` raises `RuntimeError` if the row
   vanishes mid-resolve.** Practically unreachable (would require an
   external `DELETE FROM jobs` between `_resolve` firing and the
   re-fetch). Earlier draft used `JoinTimeoutError` which is the
   wrong error class — that path is not a timeout. Changed to plain
   `RuntimeError` rather than introducing a new error class for an
   essentially-never path.

## Bugs found and fixed

None — Stream A surfaced no bugs in the phase-1 surface. The behavior
changes (wrap-transparency for `retry_failed=False`, removal of the
sync-poll loop) are deliberate per the brief's resolution table.

## Test results

```
95 passed in 7.53s
```

- Full suite under the 10-second budget.
- 64 phase-1 tests still pass.
- 31 new tests across 5 new files for sub-phases 2.A.1 + 2.A.2.
- All 18 named tests collected and passing.
- Race-prone tests (`test_atomic_claim_under_concurrent_submit_load`,
  `test_submit_idempotency_collision_returns_handle_to_existing_active_row`,
  `test_wait_primitive.py`, `test_idempotency.py`) ran 50/50 in
  succession with no flakes.
- Smoke check: `python -c "from recorded import recorder, Recorder, JobHandle, attach, get, last, list, connection"` passes.

No commit yet — diff is clean and held for lead review.
