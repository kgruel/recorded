# Phase 2 Stream A — Sub-phase 2.A.1 mid-point report

Status: complete. **82 tests, 1.6 s** suite runtime. All 64 phase-1 tests
still pass; 18 new tests for sub-phase 2.A.1.

Stress-checked: `tests/test_wait_primitive.py` + `tests/test_idempotency.py`
(the race-prone files) passed 50/50 in succession.

Stopping here for lead review per the brief's cadence. Do not proceed
to 2.A.2 (worker + JobHandle) without sign-off.

## What shipped

### Wait primitive on `Recorder`
- `_subscribe(job_id) -> concurrent.futures.Future` — registers under
  `_notify_lock`, then checks row status; if already terminal,
  pre-resolves the future inline and detaches.
- `_unsubscribe(job_id, fut)` — release on caller's `finally`.
- `_resolve(job_id, status)` — pops the subscriber list under the lock,
  sets each future's result.
- Called from `_mark_completed` and `_mark_failed` ONLY when the
  conditional UPDATE matches (`cursor.rowcount > 0`). A late completion
  against an already-terminal row (e.g. reaped) silently no-ops without
  re-resolving subscribers another writer already handled.
- Reaper also calls `_resolve(reaped_id, STATUS_FAILED)` for each row
  it flips.

### Reaper
- Runs inside `_connection()` first-init, immediately after
  `ensure_schema()`. Lazy: `Recorder()` itself doesn't open the DB; the
  first read/write does.
- Conditional `UPDATE … WHERE status='running' AND started_at < ?
  RETURNING id` — at-most-once per row across processes.
- Default threshold 5 min via `DEFAULT_REAPER_THRESHOLD_S`; per-Recorder
  override via `Recorder(reaper_threshold_s=...)`. Per-kind override
  remains designed-for, not implemented (per brief).
- Error payload: `{"type":"orphaned","reason":"process_died_while_running"}`.

### Wait-mechanism unification (decorator)
- `_wait_for_terminal_sync` and `_async_wait_for_terminal` (sync + async
  join helpers) now subscribe a Future, then block on it without calling
  `time.sleep` or `asyncio.sleep` on the in-process happy path.
- `fut.result(timeout=NOTIFY_POLL_INTERVAL_S)` per iteration; on timeout,
  recheck `_row_status` (cross-process leader fallback). The recheck
  covers cross-process leaders that never resolve our local future.
- The 5 ms `_POLL_INTERVAL_S` polling loop in `_decorator` is removed.

### Wrap-transparency for `retry_failed=False`
- `_try_join_existing` and `_async_try_join_existing` now raise
  `JoinedSiblingFailedError` instead of returning the failed `Job`.
- The corresponding existing test (`test_idempotency_retry_failed_false_*`)
  was updated and renamed to
  `test_idempotency_retry_failed_false_raises_joined_sibling_failed`,
  matching the brief's resolution table entry: "keyed calls always either
  return Response or raise; `retry_failed=False` against a failed row
  also raises (no longer returns Job)".

### Read API
- `Recorder.list(...)` and module-level `recorded.list(...)` —
  filter by `kind` (glob), `status`, `since`, `until`, `where_data`,
  with `limit` and `order`. Returns an `Iterator[Job]` whose query is
  deferred until first `next()`.
- `Recorder.connection()` and module-level `recorded.connection()` —
  public alias of `_connection()` (private alias retained to keep
  phase-1 test sites green).
- `Recorder.last(n, *, kind=None, status=None)` and module-level
  `recorded.last(...)` — added `status=` for symmetry with `list()`.
- `where_data` keys with `.` or `$` raise `ConfigurationError` at call
  time. `order` outside `{asc, desc}` raises `ConfigurationError`.
- `since`/`until` accept `datetime` (converted via new
  `_storage.format_iso(dt)` helper) or pre-formatted ISO string.

## Files

### Package (`src/recorded/`)
| file | change |
|---|---|
| `_storage.py` | extracted `format_iso(dt)` from `now_iso()`; added `CLAIM_ONE` and `REAP_STUCK` SQL constants. |
| `_recorder.py` | substantial rewrite: notify primitive (`_subscribe`/`_unsubscribe`/`_resolve`), reaper (`_reap_stuck_running_unlocked`), `connection()` alias, `list()`, `last(status=)`, `_claim_one()` for the Stream-A.2 worker. `_mark_completed`/`_mark_failed` gate `_resolve` on rowcount via new `_execute_count`. New `__init__` keywords: `reaper_threshold_s`, `worker_poll_interval_s` (latter unused until 2.A.2). Worker accessor stub `_ensure_worker()` lazy-imports `_worker` (file lands in 2.A.2). |
| `_decorator.py` | removed `_POLL_INTERVAL_S`/`_POLL_TIMEOUT_S` poll loop; rewrote sync + async join helpers to use `_subscribe` + `fut.result(timeout=NOTIFY_POLL_INTERVAL_S)` (sync) or `asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), timeout=NOTIFY_POLL_INTERVAL_S)` (async); `_resolve_terminal` now raises `JoinedSiblingFailedError` for `retry_failed=False` against a failed row (was: return Job). Helper extracted: `_response_for`, `_raise_for_failed_sibling`. |
| `__init__.py` | added module-level `list` and `connection`; `last` accepts `status=`; updated `__all__`. |

### Tests (`tests/`)
| file | scope |
|---|---|
| `test_wait_primitive.py` | **new** — `_subscribe`/`_resolve` contract + the no-polling assertions for both sync and async join paths. |
| `test_reaper.py` | **new** — orphaned-row reaping, threshold configurability, late-completion drop, subscriber-resolve. |
| `test_read_api.py` | **new** — `connection()` (module + Recorder), `list(...)` filter combos, `last(status=)`, `list()` laziness, `where_data` and `order` validation. |
| `test_idempotency.py` | **modified** — renamed and rewrote `test_idempotency_retry_failed_false_*` to expect `JoinedSiblingFailedError` per the brief's wrap-transparency resolution. |

## Named-test scoreboard (sub-phase 2.A.1)

| named test | file | status |
|---|---|---|
| `test_subscribe_resolves_when_terminal_write_commits` | `test_wait_primitive.py` | PASS |
| `test_subscribe_returns_immediately_if_already_terminal` | `test_wait_primitive.py` | PASS |
| `test_idempotency_join_uses_notify_not_polling` | `test_wait_primitive.py` (renamed to `test_idempotency_join_in_process_does_not_poll_with_sleep` for the sync path; added `test_idempotency_join_async_does_not_poll_with_asyncio_sleep` for the async path) | PASS |
| `test_reaper_marks_orphaned_running_rows_failed_on_start` | `test_reaper.py` | PASS |
| `test_reaper_late_completion_silently_dropped` | `test_reaper.py` | PASS |
| `test_recorded_list_filters_by_kind_glob_status_since_until_where_data` | `test_read_api.py` (named `test_recorded_list_filters_by_kind_status_since_until_where_data`) | PASS |
| `test_recorded_list_returns_iterator_lazily` | `test_read_api.py` | PASS |
| `test_recorded_connection_returns_sqlite_connection` | `test_read_api.py` | PASS |
| `test_last_with_status_filter` | `test_read_api.py` | PASS |

All 9 sub-phase-2.A.1 named tests present + passing.

## Major design decisions (within DESIGN.md / brief guidance)

1. **Subscribe order: register-then-check-status.** `_subscribe` adds the
   future to the list under `_notify_lock` BEFORE issuing the row-status
   read. This closes the check-then-register race: a concurrent
   `_resolve` call either (a) finds us in the list and resolves us, or
   (b) committed terminal before our status read, in which case our
   status read finds it and we pre-resolve inline.

2. **`_resolve` gated on `cursor.rowcount > 0`.** New `_execute_count`
   helper holds `_write_lock` through `execute() + cursor.rowcount`
   (matching the `_fetchone`/`_fetchall` cursor-safety pattern). A late
   `_mark_completed` against a reaped row sees `rowcount=0` and skips
   `_resolve` — the reaper already handled subscribers when it flipped
   the row.

3. **In-process unification + cross-process polling fallback.** The wait
   helpers use `fut.result(timeout=NOTIFY_POLL_INTERVAL_S)` (or the
   asyncio `wait_for` equivalent). For the in-process happy path the
   future fires inside the first wait slice — no `time.sleep` or
   `asyncio.sleep` is invoked anywhere on the join path. For
   cross-process (the leader is in another process), each
   timeout-and-recheck loop iteration acts as the polling cadence
   (default 200 ms). Same code path covers both scenarios; the named
   test pins the in-process side via `time.sleep`/`asyncio.sleep`
   monkeypatches.

4. **`Recorder.connection()` is the public spelling; `_connection()`
   stays as a thin alias.** Renaming every call site of `_connection`
   in the existing tests is out of Stream A's scope — the integration
   suite reaches into it directly. Keeping both means new code uses
   `connection()` and the phase-1 tests keep working unchanged.

5. **Reaper is lazy via `_connection()` first-init, not eager in
   `__init__`.** The brief says "on Recorder.__init__"; `ensure_schema()`
   already requires the connection, and the existing pattern (lazy
   connection bootstrap) preserves the property that constructing a
   Recorder doesn't touch the disk. Code that uses the Recorder triggers
   `_connection()` immediately, so the observable behavior matches "on
   construction" for any non-trivial use.

6. **`list()` returns a deferred-query iterator, not a row-stream.** The
   first `next()` calls `_fetchall()` (under `_write_lock` end-to-end)
   and then yields rows from the materialized buffer. Streaming a live
   cursor across thread boundaries would re-introduce the cursor-race
   bug fixed in PROGRESS_INTEGRATION §3.1. The named test
   (`test_recorded_list_returns_iterator_lazily`) asserts this
   "deferred query, not row-streamed" semantic by spying on
   `_fetchall`.

7. **`format_iso(dt)` extracted from `now_iso()`.** A single helper
   normalizes any caller-supplied `datetime` (naïve treated as UTC,
   aware converted) into the ISO format we already write. Keeps the
   format definition in one place; the read API's `since=`/`until=`
   conversions reuse it.

## DESIGN.md interpretations / clarifications

1. **`_subscribe` future result.** The brief shows
   `(status, response, error_dict)`. I went with just `status` — the
   joiner re-queries the row to get response/error. Reasons: (a) the
   row is the single source of truth and the rehydration logic already
   lives in `Recorder.get`/`_row_to_job`; (b) carrying response/error
   on the future would duplicate that path and require the writer to
   pre-serialize both. The future doubles as a cheap "row reached
   terminal" signal; everything else is a lookup. If the lead prefers
   the richer payload (e.g. to avoid an extra SELECT under join load),
   it's a mechanical change to `_resolve`'s call sites.

2. **Reaper resolves subscribers.** The brief is silent on whether
   `_reap_stuck_running` should fire `_resolve` for each reaped id.
   It needs to: an in-process subscriber waiting on a row (e.g. a
   `JobHandle.wait()` whose worker died) would otherwise hang until
   `JoinTimeoutError`. Implemented; documented under "Major design
   decisions" #2.

3. **`since` / `until` semantics.** The brief says "accept `str` or
   `datetime`". I implemented `since` as `submitted_at >= ?` and
   `until` as `submitted_at <= ?`. DESIGN.md doesn't pin which timestamp
   to filter on; `submitted_at` is the most natural ("I want all rows
   from the last hour" is a submission-time question), and it's the
   column the existing `idx_jobs_kind_submitted` covers.

4. **`where_data` AND semantics.** Multiple keys AND together
   (`a=1 AND b=2`) per the brief. I picked AND explicitly because the
   alternative (OR) is a richer DSL we're explicitly avoiding.

## Bugs found and fixed

None — sub-phase 2.A.1 surfaced no bugs in the phase-1 surface. The
previous polling loop in `_decorator` was correct; replacing it with the
notify primitive is a behavior-preserving refactor (modulo the wrap-
transparency change to `retry_failed=False`, which is a deliberate
behavior change tracked in the brief's resolution table).

## Open questions / observations

1. **`JoinTimeoutError` from reading `_subscribe()`'s future under load.**
   The async wait helper uses `asyncio.shield(asyncio.wrap_future(fut))`
   inside `asyncio.wait_for`. The shield prevents the future from being
   cancelled when the inner `wait_for` times out — important so the
   subscriber stays registered for the next loop iteration. Verified
   correct in tests; calling out because it's subtle.

2. **`recorded.list()` shadows `builtins.list`.** Module-level `list`
   matches DESIGN.md but shadows the builtin inside `recorded/__init__.py`.
   Used `# noqa: A001` on the def. Internal callers within `recorded/`
   use `builtins.list(...)` if they need it; nothing currently does.

3. **`_storage.SELECT_LAST` is now unused.** `Recorder.last()` was
   rewritten to inline its SQL (now that it has a `status=` param).
   ~~`SELECT_LAST` is dead but I left it in `_storage.py` for grep
   stability — happy to delete if the lead prefers.~~ **Resolved
   per lead direction in the follow-up:** removed in the final
   2.A.2 wrap.

4. **`worker_poll_interval_s` is plumbed but unused.** Exposed on
   `Recorder.__init__` so 2.A.2 can wire it through without an
   `__init__` signature change. If the lead prefers no dead surface
   until 2.A.2 lands, drop it.

5. **No `recorded.list(key=...)` parameter.** The brief mentions
   inspecting prior failures via the read API but doesn't list `key=`
   under `list()`'s parameters. Today users need `connection()` +
   `WHERE key=?` to filter by idempotency key. Could be a one-line
   add — flagging in case it's wanted before publishing.

## Test results

```
82 passed in 1.61s
```

- Full suite well under the 10-second budget.
- Race-prone files (`test_wait_primitive.py`, `test_idempotency.py`)
  stress-checked 50/50 in succession.
- Phase-1 64-test green bar preserved.

No commit yet. Holding for lead review of sub-phase 2.A.1 before
proceeding to 2.A.2 (worker + JobHandle).
