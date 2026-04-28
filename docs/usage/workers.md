# Background execution

`wrapper.submit(...)` enqueues a job for execution by a *cross-process leader*.
The call returns immediately with a `JobHandle`; the leader process picks up
the row and runs the wrapped function. `.submit()` requires a leader to be
running — otherwise it raises `ConfigurationError`.

> Architecture context: [`docs/HOW.md` — submitted-call trace](../HOW.md#trace-a-call-submitted-path)

## The two-process model

In production, your application is split across at least two processes that
share the same `jobs.db`:

| Process | What it does |
|---|---|
| **App process(es)** — your FastAPI / CLI / scripts | Defines `@recorder` functions, calls `fn.submit(req)` to enqueue work, calls `handle.wait()` to collect results. |
| **Leader process** — `python -m recorded run` | Imports the same modules (`--import myapp.tasks`), claims `pending` rows, runs the wrapped functions, writes terminal status. |

`.submit()` is the cross-process tier. If you don't need cross-process handoff,
call the function bare (without `.submit()`) — that path runs inline, records
the row, and doesn't require any leader infrastructure.

## Running a leader

```bash
# Production: run as a long-lived process under your supervisor of choice
python -m recorded run --path /var/lib/myapp/jobs.db --import myapp.tasks

# Multiple --import flags supported
python -m recorded run --path jobs.db --import myapp.tasks --import myapp.workflows
```

Flags:

| Flag | Default | Purpose |
|---|---|---|
| `--path` | `./jobs.db` | SQLite DB path. Must match the app process. |
| `--import MOD` | (none) | Import this module before claiming. Run once per `@recorder`-defining module. |
| `--shutdown-timeout S` | `10.0` | On SIGTERM/SIGINT, wait this long for in-flight tasks to drain before cancelling. |

The leader prints `recorded: leader started (host:pid, leader_id=...)` on stdout
when ready, and `recorded: leader stopped (...)` when it exits cleanly. SIGTERM
or SIGINT triggers graceful shutdown: stop claiming new rows, drain in-flight,
DELETE the heartbeat row, exit 0.

Multiple leaders against the same DB is allowed (e.g. for capacity); each
claims independently via `UPDATE ... RETURNING`, and SQLite WAL serializes
writers so the same row never executes twice.

## Submitting a job

```python
from myapp.tasks import build_report

handle = build_report.submit(spec, key="daily-2026-04-27")
# returns immediately; build_report has not yet run.
```

`.submit(...)` requires a **single positional argument** (the request),
plus optional `key=` and `retry_failed=` keywords. It first checks that
a leader is running (`is_leader_running()`), then writes a `pending` row
and returns a `JobHandle`. Multi-argument and keyword-only call shapes
are rejected: the cross-process envelope must be reconstructable by the
leader, which is straightforward for one positional value and ambiguous
otherwise. For multi-argument functions, define a `request=Model` that
captures the call shape and pass that one model instance to `.submit()`,
or use the bare-call form (in-process execution, accepts the function's
native call shape).

If the call site collides with an active key, `.submit()` returns a `JobHandle`
over the existing row instead of inserting a new one — same idempotency
semantics as a bare-call keyed join.

### Loud failure: no leader

```python
build_report.submit(spec)
# ConfigurationError: 'reports.build'.submit() requires a running leader
# process. Start one with `python -m recorded run --path './jobs.db'
# --import <module>` (where <module> is the package whose @recorder
# decorations include this kind), or call the function bare (without
# .submit()) for in-process execution. Probe via
# `recorder.is_leader_running()` for health checks.
```

A misconfigured deployment fails on the first `.submit()` call rather than
silently degrading. The error message names the fix.

## Health checks

```python
import recorded

if not recorded.get_default().is_leader_running():
    abort_with("backend leader not available")
```

Use `Recorder.is_leader_running()` in `/health` endpoints or supervisor
liveness probes to surface "no leader" before requests start hitting the
`.submit()` gate.

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
# ... process exits, request returns to client, etc. ...

# Later, from another process or a fresh run:
job = recorded.get(handle.job_id)  # if you saved the id
# or
jobs = list(recorded.query(kind="reports.build", key="daily-2026-04-27"))
```

## Leader heartbeat

The leader inserts a single `_recorded.leader` row keyed by `host:pid` on
startup, and updates its `started_at` every `leader_heartbeat_s` seconds
(default 5 s). `is_leader_running()` is a one-row probe: any
`_recorded.leader` row whose `started_at` is within `leader_stale_s`
(default 30 s) is fresh.

`_recorded.*` is a reserved kind prefix; user code can't decorate a function
with `kind="_recorded.foo"` (rejected at decoration time), and the read API
(`Recorder.query`/`last`, module-level `recorded.query`/`last`) hides
heartbeat rows by default. Pass `kind="_recorded.*"` to opt into seeing
them.

If a leader process becomes unresponsive past the reaper threshold (default
5 min, much larger than `leader_stale_s`), its heartbeat row gets reaped to
`failed` automatically — same machinery as any other stuck `running` row.
The leader notices on its next `_touch_leader_heartbeat` (rowcount = 0),
re-claims a fresh slot, and emits a dual-channel `RecordedWarning`
(see [errors](errors.md) and `docs/HOW.md::Warnings policy`).

## The reaper

A defensive sweep for orphaned `running` rows. Runs on
`Recorder._connection()` first-init — i.e., the first SQLite touch after
the recorder is constructed.

```sql
UPDATE jobs SET status='failed',
                completed_at=?,
                error_json='{"type":"orphaned",...}'
 WHERE status='running' AND started_at < ?  -- now - threshold
RETURNING id;
```

Default threshold: 5 minutes (`DEFAULT_REAPER_THRESHOLD_S`). Configure via:

```python
recorded.configure(reaper_threshold_s=600.0)  # 10 minutes
```

Why it exists: if a leader process crashes mid-run, its `running` row is
orphaned. Without the reaper, the partial unique index treats it as still
holding the `(kind, key)` slot — new keyed calls would join it forever.
The reaper flips it to `failed`, releasing the slot. Dead leader heartbeat
rows get cleaned up the same way.

The reaper is multi-process safe. The conditional `WHERE status='running'`
ensures at-most-once-per-row even if multiple recorders boot against the
same DB. Late completion against a reaped row is silently dropped.

## When you don't need `.submit()`

If you don't need cross-process handoff and just want recording, use the
bare call:

```python
build_report(spec)              # bare — runs inline, recorded, no leader needed
# vs
build_report.submit(spec)       # submitted — leader process runs it
```

The bare path doesn't query `is_leader_running()` (Tier 1 stays free of
leader-detection cost). It also takes one fewer write
(`running → terminal` for bare; `pending → running → terminal` for
submitted — `pending` exclusively means "queued for the leader").
