# Phase 2 Stream C — Sub-phase 2.C.1 mid-point report

Status: complete. 103 tests, 3.2 s suite runtime. All 7 named tests for
2.C.1 collected and passing. Phase-1 + Stream A suite (96 tests) still
green.

## Files touched

### Package (`src/recorded/`)

| file | change |
|---|---|
| `__init__.py` | Re-export `configure`. Added to `__all__`. |
| `_recorder.py` | New `join_timeout_s` keyword on `Recorder.__init__` (default `DEFAULT_JOIN_TIMEOUT_S`); stored as `self.join_timeout_s`. New `__aenter__`/`__aexit__` (FastAPI lifespan). New module-level `configure(...)`: configure-once, only forwards explicitly-set kwargs, registers `atexit.register(r.shutdown)` on the configured default. `_set_default(...)` docstring updated to call out the atexit asymmetry. New module state `_atexit_registered_for: set[int]` to guard against double-registration. |
| `_handle.py` | `JobHandle.wait()` / `wait_sync()` read `self._recorder.join_timeout_s` when `timeout is None`. Dropped the `DEFAULT_JOIN_TIMEOUT_S` import; the constant now reaches handles only via `Recorder.__init__`'s default. |
| `_decorator.py` | The two idempotency-join helpers (`_wait_for_terminal_sync`, `_async_wait_for_terminal`) now pass `recorder_inst.join_timeout_s` to their `_await_*` workhorses instead of the module constant. Same import cleanup as `_handle.py`. |

### Tests (`tests/`)

| file | scope |
|---|---|
| `test_configure.py` | **new** — all 7 named tests for 2.C.1. Local autouse fixture clears `_default` before/after each test so `configure-once` semantics don't bleed across tests. |

### Project files

| file | role |
|---|---|
| `PROGRESS_2_C_1.md` | This report. |

## Named-test scoreboard (2.C.1)

| named test | file | status |
|---|---|---|
| `test_configure_returns_recorder_with_given_path` | `test_configure.py` | PASS |
| `test_configure_is_no_op_after_first_call` | `test_configure.py` | PASS |
| `test_configure_sets_join_timeout_seen_by_handle_wait_default` | `test_configure.py` | PASS |
| `test_configure_join_timeout_does_not_override_explicit_per_call_timeout` | `test_configure.py` | PASS |
| `test_configure_join_timeout_reaches_idempotency_join_helpers` | `test_configure.py` | PASS |
| `test_atexit_runs_shutdown_on_configured_default_only` | `test_configure.py` | PASS |
| `test_recorder_async_context_manager_bootstraps_and_shuts_down` | `test_configure.py` | PASS |

## Major design decisions made within DESIGN.md's guidance

1. **Configure-once via "first wins, ignore later kwargs."** A second
   `configure(path=..., join_timeout_s=...)` returns the existing
   default with no warning, no raise, no field swap. This matches the
   brief's guidance ("no warning, no raise") and the docstring spells
   out that tests / explicit-instance code must use `Recorder(...)`
   directly to get a different shape.

2. **Only-forward-set-keywords.** `configure()` builds the kwargs dict
   from the args the caller actually supplied (skipping `None`-valued
   ones), then calls `Recorder(**kwargs)`. This way a future
   `Recorder.__init__` default change (e.g. bumping the reaper
   threshold) is picked up by `configure()` callers automatically;
   we never silently freeze the current defaults into `configure()`'s
   call chain.

3. **`atexit` registered only on the configured default — direct
   `Recorder(...)` doesn't.** The brief's guidance ("Only the
   *configured* default registers atexit. Document.") is honored:
   `Recorder.__init__` doesn't register atexit; `_set_default(...)`
   doesn't either; `configure()` does. Documented in `_set_default`'s
   docstring and `configure()`'s docstring. Test suite would otherwise
   accumulate atexit registrations for hundreds of test-fixture
   recorders.

4. **`_atexit_registered_for: set[int]` guard.** Configure-once
   already guarantees we never register twice for the same default,
   but the guard makes the property local to the registration site
   rather than relying on configure-once invariants holding forever.
   Cheap belt-and-suspenders.

5. **`__aenter__` / `__aexit__` use `asyncio.to_thread`.** `_connection()`
   (with the reaper sweep) and `shutdown()` are blocking calls; running
   them on the loop thread under FastAPI lifespan would block any
   concurrently-starting task. The `to_thread` wrap matches every other
   "SQLite work from an async context" callsite in the package.

6. **`JobHandle.wait()` reads `recorder.join_timeout_s` lazily on each
   call, not at handle construction.** A handle constructed before
   `configure()` (unlikely but possible in test paths that swap the
   default) still picks up the configured timeout the first time
   `.wait(timeout=None)` runs.

7. **The two idempotency-join helpers continue to pass `timeout_s`
   into the underlying `_await_*` workhorse rather than reading
   `recorder_inst.join_timeout_s` inside the workhorse.** Keeps the
   workhorse signature explicit; the per-call `timeout=` plumbing
   from `JobHandle.wait(timeout=...)` already uses the same shape.
   Per-call wins over per-Recorder, per-Recorder wins over module
   constant.

## DESIGN.md interpretations / clarifications

1. **Reaper still runs on connection bootstrap, not on `__aenter__`.**
   `__aenter__` calls `_connection()`, which is the existing reaper
   trigger. So FastAPI lifespan will sweep stuck rows on app startup
   without us adding a separate reaper-on-aenter path.

2. **`configure(path=None)` uses the `Recorder.__init__` default
   (`./jobs.db`).** DESIGN.md's "DB path resolution: explicit
   `Recorder(path=...)` → `recorded.configure(path=...)` → `./jobs.db`"
   is preserved by the only-forward-set-keywords approach: passing no
   path to `configure()` lets `Recorder.__init__` pick its default.

3. **Atexit subprocess test asserts no `-wal` / `-shm` files remain
   after exit.** A clean SQLite WAL shutdown checkpoints and removes
   the auxiliary files; their presence after exit would indicate the
   connection was abandoned without `close()` (i.e. atexit didn't
   run). This is a much sharper signal than "exit code 0" alone (an
   abandoned connection still exits 0 in CPython).

## Deviations from the brief

1. **The atexit test is written as an in-process `subprocess.run([sys.executable, "-c", ...])`
   one-shot, capped at 5 s, exactly as the brief specified.** No
   deviation; flagged here so the lead doesn't have to re-read the brief
   to confirm — the assertion shape is "exit 0 + no stale `-wal`/`-shm`
   files in tmp_path".

2. **No new `_set_default` semantics.** I considered making
   `_set_default(r)` also register atexit when `r is not None`, but
   the brief's "Only the *configured* default registers atexit"
   wording made the choice — and the test fixtures' bulk usage of
   `_set_default` would otherwise register hundreds of atexit handlers
   per test session.

## Open questions / rough spots

1. **`configure()` returning the existing default silently on a second
   call.** A loud-on-second-call variant (`ConfigurationError` if
   `_default is not None`) would catch app-startup misconfiguration
   bugs (two competing `configure()` calls in different module-init
   paths). The brief's resolved-decision row says "no warning, no
   raise" so I went with silent. Easy to flip later if the lead wants
   a stricter contract — the change would be one branch in `configure()`
   plus a test-fixture-friendly bypass (the fixture already uses
   `_set_default(None)` which would clear the guard).

2. **`configure()` does NOT take an arbitrary `recorder=` instance
   argument.** A caller who wants to install a pre-built `Recorder`
   as the default still has to use `_set_default(r)` (private). DESIGN.md
   doesn't ask for this, the brief doesn't either, and Stream C's
   ~150-LOC budget is tighter than carrying an extra constructor mode.
   Flag in case the lead wants to surface a public `install(r)` later.

3. **`__aenter__` constructs the connection and runs the reaper.**
   Stream A made the reaper part of `_connection()` first-init. If a
   FastAPI app calls `recorded.configure(...)` at module import time,
   the FIRST time the connection opens is at the first decorator
   invocation, NOT at `__aenter__`. Whether that ordering matters is
   subtle: the brief's example shows `async with recorded.configure(...)
   as recorder` meaning the connection opens during lifespan startup,
   which is what app authors want. If they instead bare-call
   `recorded.configure(...)` at module init they get connection-opens-
   on-first-call semantics. Both work; the difference is just startup
   timing.

4. **`__aexit__` swallowing exceptions vs. re-raising.** Right now it
   doesn't return anything (so it doesn't suppress the exception that
   triggered the exit). It just runs `shutdown()` then lets the
   exception propagate. This matches `__exit__`'s shape.

## Test results

```
103 passed in 3.20s
```

- Full suite under the phase-2 12 s budget.
- All 7 named tests for 2.C.1 collected and passing.
- All 96 prior tests still pass.
- Atexit subprocess test runs at ~0.05 s wall-clock (well inside the
  5 s cap).

No commit yet — diff is clean and held for lead review per the brief.

Stopping for review before starting 2.C.2.
