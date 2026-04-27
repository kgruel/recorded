# Phase 1 — Integration test report

Status: complete. **15 new tests, 1.6 s suite runtime** (full project suite: 64
tests, ~1.6 s). All 49 prior unit tests still pass.

The integration suite drives real HTTP traffic via `pytest-httpserver` (a
WSGI server in a thread, on a random port). No transport mocking; sockets
are real. The only thing that's faked is the user's external (the broker),
which is the intended boundary for mocking in this kind of suite.

**Two real concurrency bugs surfaced** during writing — both fixed in
`src/recorded/`. Details in §3.

## 1. Files created / modified

| file | change |
|---|---|
| `tests/test_integration.py` | **new** — 15 integration tests against pytest-httpserver. |
| `pyproject.toml` | added `pytest-httpserver>=1` and `httpx>=0.27` to `[dev]` extras. |
| `src/recorded/_recorder.py` | **bugfix** — added `_fetchone` / `_fetchall` helpers that hold `_write_lock` through the fetch (existing `_execute` released the lock before the cursor was stepped). All read paths switched to the safe helpers. |
| `src/recorded/_decorator.py` | **bugfix** — added `_async_try_join_existing`, `_async_wait_for_join`, `_async_wait_for_terminal` (asyncio-aware polling) and rewired the async wrapper's idempotency-join paths to use them. Previously the busy-wait pinned a threadpool worker per joiner, which deadlocks at ≥ ~30 concurrent same-key callers. The sync wrapper still uses the original `time.sleep` loop — correct for sync callers. |

## 2. Test inventory

All in `tests/test_integration.py`. Names map 1:1 to the brief.

| # | test | covers |
|---|---|---|
| 1 | `test_fifty_concurrent_async_calls_all_recorded` | 50 gathered async POSTs; each row `completed`, timestamps in order, request/response JSON round-trips with the per-call payload. |
| 2 | `test_fifty_concurrent_idempotent_make_one_real_request` | 50 gathered same-key callers; **exactly one** request hits the server, all 50 callers see the same response, exactly one active row. The financial-safety test. |
| 3 | `test_sequential_idempotency_returns_rehydrated_without_reexecution` | A finishes; B–E join sequentially with same key. One server hit, all see A's response, recorded request is A's input (not B–E's). |
| 4 | `test_attach_trail_under_real_io` | wrapped fn awaits a real HTTP call, attaches `correlation_id` from the response body and `status_code` from the response object. `data_json` contains both. |
| 5a | `test_dataclass_adapter_records_broker_payload` | dataclass `request=` + `data=` projection over a broker-shaped response (nested dict, mixed types). |
| 5b | `test_pydantic_adapter_records_broker_payload` | same with Pydantic v2 models — duck-typed, no library import. |
| 5c | `test_plain_dict_passthrough_records_broker_payload` | no models — raw dict in/out, including `None` and `False` fields. |
| 6 | `test_server_500_marks_row_failed_and_propagates_exception` | server returns 500; `httpx.HTTPStatusError` propagates verbatim; row `failed`; `error_json` carries `{type, message}`. |
| 7 | `test_slow_response_records_realistic_duration` | 200 ms server delay; `Job.duration_ms ∈ [200, 2000)`. |
| 8 | `test_query_real_recorded_data` | `last(N)`, `last(N, kind="exact")`, `last(N, kind="ext.api.*")` glob, plus raw SQL via `Recorder._connection()` for the equivalent of `where_data={"customer_id": 7}`. |
| 9 | `test_unrecordable_response_under_load_marks_failed_and_raises_serialization` | 50 concurrent calls returning a non-JSON-serializable type; every caller sees `SerializationError` (not `TypeError`); every row `failed`. |
| 10 | `test_sync_decorator_with_sync_httpx_client` | sync wrapper, sync `httpx.Client`, full lifecycle, `last()` rehydrates request/response. |
| 11 | `test_async_run_does_not_block_loop_during_real_sync_io` | `.async_run` over a sync 200 ms HTTP call; concurrent ticker task fires ≥ 3× during the call (proves loop wasn't blocked). |
| 12 | `test_sync_from_inside_event_loop_raises` | `.sync()` inside a running loop raises `SyncInLoopError`. |
| 13 | `test_submit_raises_not_implemented_with_phase2_message` | `.submit(...)` raises `NotImplementedError` with the phase-2 message. |

## 3. Bugs found and fixed

### 3.1 — `_execute` releases the write-lock before the cursor is stepped

**Symptom:** `IndexError: tuple index out of range` from `_recorder._row_status`
(line `return None if row is None else row[0]`) on the very first run of
test #2 (50-gathered idempotent) — deterministic, surfaced on the first
test execution.

**Root cause:** `Recorder._execute` did:

```python
with self._write_lock:
    return conn.execute(sql, params)
```

The cursor is returned to the caller, and `.fetchone()` runs **after** the
lock is released. With many `asyncio.to_thread` workers concurrently
issuing executes against the shared connection, the cursor for thread A
gets stepped while thread B is mid-execute on the same connection.
SQLite's C cursor object is bound to the connection, not its Python wrapper,
and the result is a partially-stepped row coming back as an empty tuple.

**Fix:** added `_fetchone(sql, params)` and `_fetchall(sql, params)` that
hold `_write_lock` through both the execute and the fetch. All read paths
in `Recorder` (`get`, `last`, `_row_status`, `_lookup_active_by_kind_key`,
`_lookup_latest_failed`) now use them. `_execute` is kept for write-only
calls (UPDATE/INSERT) where there is nothing to fetch.

This fix matters for phase 2 too — the worker will read row state from
many tasks concurrently and would have hit this immediately.

### 3.2 — Async idempotency-join exhausts the asyncio threadpool

**Symptom:** test #2 (50-gathered idempotent) timed out at 30 s
(`JoinTimeoutError`) reproducibly after fixing 3.1.

**Root cause:** the polling loop in `_wait_for_terminal` uses `time.sleep`
and is invoked via `asyncio.to_thread(_try_join_existing, ...)` from the
async wrapper. So 49 joiners each pin one threadpool worker for the
entire poll duration. The default `asyncio` threadpool is `min(32, ...)`
workers; 49 joiners exhaust it. The single winner cannot dispatch its
`_mark_running` / response-write `to_thread` calls — its thread requests
queue forever, the row never reaches terminal, every joiner times out.

This was structurally invisible to the unit suite, which only races two
callers. It surfaces at ~30 concurrent same-key callers.

**Fix:** the async wrapper now uses `_async_try_join_existing` /
`_async_wait_for_join` / `_async_wait_for_terminal`, which:

- `await asyncio.to_thread(...)` for each individual SQL call (status check,
  lookup, rehydration), and
- `await asyncio.sleep(_POLL_INTERVAL_S)` between checks instead of
  blocking a thread.

Threadpool occupancy stays at O(1) per joiner — the writer always finds a
free slot. The sync wrapper's `_try_join_existing` / `_wait_for_terminal`
are unchanged: a sync caller is already blocking, and there's no event
loop to yield to.

Both fixes leave the existing 49-test unit suite green and don't change
any public API.

## 4. Design observations

These are correct against DESIGN.md but felt rough in practice. Flagging
for the lead — no code changes made.

1. **Polling cadence is fine for joiners but the timeout is a footgun.**
   `_POLL_TIMEOUT_S = 30.0` in the async wrapper is the upper bound any
   joining caller is willing to wait. With long-running broker calls
   (many seconds of HTTP latency, 5xx-then-retry storms), this is short
   enough to fire spuriously and erase the idempotency benefit. A
   deliberate per-call `join_timeout=` parameter, or a recorder-wide
   default scaled to the slowest expected response, would be safer. v1
   silently couples it to the polling impl.

2. **Idempotent join return-shape asymmetry is real and testable.** The
   completed-or-pending-then-completed join returns the rehydrated
   `response` (a dict / model instance). The `retry_failed=False` path
   over an existing failed row returns a `Job` instance. PROGRESS_PHASE1.md
   already flagged this — integration use confirmed it stings: the
   caller has to know which path was taken to type the result. A small
   wrapper (`if isinstance(out, Job): out = out.response_or_raise()`)
   in user code papers over it but the asymmetry should be explicit in
   the type signature.

3. **`error=Model` in the decorator surface is silently inert.**
   Several tests would have benefited from typing the broker's structured
   error envelope. We didn't try, since `_serialize_error` always emits
   `{type, message}`, but a user reading the API would expect it to
   work. Either wire it up or drop it from the surface before publishing.

4. **No `Recorder.connection()` escape hatch on the public surface.**
   Users have to reach for `recorder._connection()` (private). DESIGN.md
   names `recorded.connection()` as a top-level API. See §5.

5. **`SyncInLoopError` raises from `.sync()` correctly; `.async_run` from
   a *sync* caller (no loop) silently calls `asyncio.to_thread` outside
   any loop and would fail.** Not exercised in the test suite (the brief
   doesn't ask), but worth a small "raises if not in a loop" check
   before publishing the shim.

6. **Reaper isn't in phase 1 (by design), and the "unrecordable response"
   path leans on the wrapper to mark `failed` itself.** That path works
   (test #9 covers it under load), but if any future failure between
   `_mark_running` and the terminal write doesn't get caught (e.g. a
   coroutine cancelled, a `BaseException` like `KeyboardInterrupt`),
   the row is stuck `running`. The reaper in phase 2 closes this; until
   then, phase-1 users in long-running processes should be aware.

## 5. Query-API observations

This is the area the brief most explicitly wanted signal on. **Phase 1
ships `get()` and `last()` only.** The DESIGN.md surface for reads also
includes `recorded.list(...)` and `recorded.connection()`; neither is
implemented.

What I reached for, and what I had to substitute:

| DESIGN.md API | needed for | substituted with | comment |
|---|---|---|---|
| `recorded.list(kind="ext.api.*")` | filter rows across many kinds | `last(50, kind="ext.api.*")` | Works because `last()` already accepts a kind glob. The only missing piece is "all rows, no implicit limit" — `last(2_000_000_000, kind="...")` is the workaround. The whole point of `list()` over `last()` is that it returns an iterator and is the right verb when "the most recent N" isn't the question. |
| `recorded.list(status="failed")` | filter by terminal status | raw SQL via `_connection()` | `last()` has no `status=` parameter. Surprisingly common in practice — "show me what's broken." |
| `recorded.list(where_data={"customer_id": 7})` | equality on a `data_json` field | raw SQL via `_connection()` (hand-written `json_extract(data_json, '$.customer_id') = ?`) | Test #8 does exactly this; it works fine, but the user has to know the JSON-extract idiom and the `data_json` column name. The whole *point* of `where_data=` is to keep that knowledge in the library. |
| `recorded.list(since=..., until=...)` | time-bounded sweep | not exercised | Came up mentally while writing the suite ("run only against rows from the last test setup"). I worked around it by isolating tests with separate `db_path` fixtures. A real consumer can't. |
| `recorded.connection()` | escape hatch for raw SQL | `default_recorder._connection()` (private) | Works, but reaches into private API. Anyone doing aggregation queries (`SELECT count(*) FROM jobs WHERE …`) needs this immediately. |

**Concrete recommendations** for phase 2 (or whoever owns the read API
next):

- Ship `recorded.list(...)` with at least: `kind` (glob), `status`,
  `since`, `until`, `where_data` (equality on top-level JSON keys),
  `limit`, `order`. The DESIGN.md spec is right; phase 1 shipped a stub.
- Ship `recorded.connection()` as `Recorder.connection()` (public alias of
  `_connection()`). Zero new code; rename and document.
- Consider `last()` accepting `status=` for symmetry with `list()` —
  trivial to add (`SELECT_LAST` already takes `?` parameters).

The lack of these isn't blocking phase 1 (the data is in the DB, raw SQL
gets you anything), but it's the one place a user reading the README and
trying it would immediately notice the gap.

## 6. Perf observations

- Full integration suite: **1.6 s** locally for 15 tests (target was < 5
  s). Well inside budget.
- Slowest test: `test_async_run_does_not_block_loop_during_real_sync_io`
  at ~210 ms (the test-server `time.sleep(0.2)` is the floor).
- Second slowest: `test_slow_response_records_realistic_duration` at ~210 ms,
  same reason.
- The 50-gathered tests run in ~50 ms each after the 50 ms intentional
  hold — concurrency overhead is negligible.
- The full project suite (64 tests) runs in **1.64 s**. No warmup phase;
  every run is consistent within ±0.05 s.
- I stress-checked `test_fifty_concurrent_idempotent_make_one_real_request`
  10× back-to-back: 10/10 pass with stable timing.
- Full project suite (64 tests) run 20× back-to-back post-fix: 20/20 pass
  in 1.55–1.63 s. No flakes on any read path, including the existing
  unit-suite idempotency tests that share the now-fixed cursor-fetch
  paths.

No perf surprises. Nothing in the recorder's lifecycle path noticeably
dominates the HTTP latency.

## 7. Open questions for the lead

1. **Were the two bugs in §3 known?** They're real concurrency defects
   that the unit suite couldn't have caught (state-machine logic vs.
   live threadpool behavior). Both fixes are minimal and don't change
   public behavior, but the lead may prefer to land the fixes as
   separate commits / PRs from the test suite for cleaner history.

2. **`_async_*` join helpers — name and location.** I dropped them at
   the bottom of `_decorator.py` next to the sync versions for symmetry.
   If phase 2 is going to introduce a worker that also needs async-aware
   waits, it might be worth pulling `_async_wait_for_terminal` into a
   dedicated `_join.py` rather than have the decorator own it.

3. **Should test #2's "50 concurrent same-key" be the new floor for the
   stress check?** PROGRESS_PHASE1.md mentioned a 50× sequential run of
   the gather-race idempotency test as evidence of stability. The new
   test is a structurally stronger check (50 *parallel* same-key
   callers, with fixed-rate threadpool ceiling); worth promoting it as
   the canary. I ran it 10× without flake.

4. **The `data=Model` projection only fires for `dict` responses.**
   `_decorator._build_data_json` checks `isinstance(response, dict)`.
   If a user `response=Model` makes the response a model instance, the
   `data=` projection silently no-ops. Test #5a/b passed because the
   broker returns a dict. Worth noting for whoever wires `error=Model`
   that the same gap will exist — projection from non-dicts isn't
   defined in DESIGN.md or implemented.

5. **No commit yet.** Per the brief: stopping here. Diff is clean —
   `pyproject.toml` + new test file + two surgical bugfixes in the
   library.
