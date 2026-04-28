# Background execution

Use `wrapper.submit(...)` to enqueue a job for asynchronous execution. The
call returns immediately with a `JobHandle`; a background worker picks up
the row and runs the wrapped function on a dedicated asyncio loop.

> Architecture context: [`docs/OVERVIEW.md` — submitted-call trace](../OVERVIEW.md#trace-a-call-submitted-path)

## Submitting a job

```python
@recorder(kind="reports.build")
def build_report(spec: ReportSpec) -> Report:
    ...  # slow

handle = build_report.submit(spec, key="daily-2026-04-27")
# returns immediately; build_report has not yet run.
```

`.submit(...)` accepts the same positional/keyword arguments as the wrapped
function, plus `key=` and `retry_failed=`. It writes a `pending` row,
ensures the worker is started, and returns a `JobHandle` bound to the row.

If the call site collides with an active key, `.submit()` returns a
`JobHandle` over the existing row instead of inserting a new one — same
semantics as a bare-call idempotency join.

## `JobHandle.wait` and `wait_sync`

```python
# From async code
job = await handle.wait(timeout=60.0)

# From sync code
job = handle.wait_sync(timeout=60.0)
```

Both return the terminal `Job` on success. On the failure path they raise
`JoinedSiblingFailedError` (per the wrap-transparency principle: handle
surfaces never return a `Job` for failure paths).

`timeout=` defaults to the recorder's `join_timeout_s` (default 30 s).
`JoinTimeoutError` (also a stdlib `TimeoutError`) fires if the row hasn't
reached terminal status in time.

`wait_sync()` from inside a running event loop raises `SyncInLoopError` —
use `await handle.wait()` instead.

You don't have to wait. The recorded row is durable; come back later via the
read API:

```python
handle = build_report.submit(spec, key="daily-2026-04-27")
# ... process exits or moves on ...

# Later, from another process or a fresh run:
job = recorded.get(handle.job_id)  # if you saved the id
# or
jobs = list(recorded.list(kind="reports.build", key="daily-2026-04-27"))
```

## The worker

The worker is **lazy**. It doesn't spawn until the first `.submit()` call on
any decorated function. Subsequent submits reuse the same worker.

Architecture: one asyncio loop running in a dedicated thread. The loop polls
for `pending` rows via an atomic claim:

```sql
UPDATE jobs SET status='running', started_at=?
 WHERE id IN (SELECT id FROM jobs WHERE status='pending'
              ORDER BY submitted_at LIMIT 1)
RETURNING ...
```

This `UPDATE ... RETURNING` is the multi-process-safe claim primitive —
SQLite WAL serializes writers, and only one worker can transition any given
row from `pending` to `running`.

For each claimed row, the worker spawns an asyncio task that sets the
`current_job` ContextVar (so `attach()` and `attach_error()` work), then
runs the wrapped function and writes the terminal status. Sync functions
run on the threadpool via `asyncio.to_thread` so they don't block the
worker loop.

## Worker poll interval

The worker checks for new `pending` rows every `worker_poll_interval_s`
(default 200 ms). Configure via:

```python
recorded.configure(worker_poll_interval_s=0.05)  # 50 ms — more responsive
```

For most workloads the default is fine. Tune lower if you want faster
pickup of submitted jobs; tune higher if the worker thread shows up as
a hot loop in profiling.

In-process callers waiting on `JobHandle.wait()` don't pay the poll
interval — the wait primitive notifies them the moment the terminal write
commits.

## Worker shutdown

`recorder.shutdown()` (called automatically via `atexit` after
`configure()`, or explicitly via the `with` block on a `Recorder`) drains
the worker:

1. Mark the recorder closed (blocks new `_ensure_worker()` calls).
2. Signal the worker loop to stop accepting new claims.
3. Wait for in-flight tasks to finalize their terminal writes.
4. Close the SQLite connection.

In-flight tasks complete normally — their terminal writes (`_mark_failed` /
`_mark_completed`) are allowed against the still-open connection during
the drain phase. Pending rows that haven't been claimed yet remain in
`pending` and will be picked up by the next worker that starts (this
process or another).

## The reaper

A defensive sweep for orphaned `running` rows. Runs on
`Recorder._connection()` first-init — i.e., the first SQLite touch after
the recorder is constructed.

```sql
UPDATE jobs SET status='failed',
                completed_at=?,
                error_json='{"type":"ReapedAfterCrash",...}'
 WHERE status='running' AND started_at < ?  -- now - threshold
RETURNING id;
```

Default threshold: 5 minutes (`DEFAULT_REAPER_THRESHOLD_S`). Configure via:

```python
recorded.configure(reaper_threshold_s=600.0)  # 10 minutes
```

Why it exists: if a worker process crashes mid-run, its `running` row is
orphaned. Without the reaper, the partial unique index treats it as still
holding the `(kind, key)` slot — new keyed calls would join it forever.
The reaper flips it to `failed`, releasing the slot.

The reaper is multi-process safe. The conditional `WHERE status='running'`
ensures at-most-once-per-row even if multiple recorders boot against the
same DB. Late completion against a reaped row is silently dropped (the
original writer's `UPDATE ... WHERE status='running'` no longer matches,
`cursor.rowcount == 0`, no notify fires).

The reaper also resolves any subscribers waiting on the row via the wait
primitive — joiners get unblocked rather than hanging waiting for a dead
leader.

## Atomic claim, multi-process

Multiple worker processes against the same `jobs.db` is supported. Each
worker runs its own atomic-claim loop; SQLite WAL serializes writers.

There's no leader election. Each pending row goes to whichever worker
claims it first. Crash of one worker means its in-flight rows hit the
reaper threshold and get failed; other workers continue normally.

## When you don't need `.submit()`

If you don't care about background execution and just want recording, use
the bare call:

```python
build_report(spec)              # bare — runs inline, recorded
# vs
build_report.submit(spec)       # submitted — worker runs it asynchronously
```

The bare path doesn't spawn the worker. It also takes one fewer write
(`pending → running → terminal` for submitted; `running → terminal` for
bare — `pending` exclusively means "queued for the worker").
