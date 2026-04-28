"""`recorded.configure(...)` + Recorder lifecycle additions (Stream C 2.C.1).

Configure-once semantics, `join_timeout_s` plumbing through both the
`JobHandle.wait()` default and the idempotency-join helpers, atexit
registration on the configured default, and the `async with Recorder(...)`
context manager wired for FastAPI lifespan.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

import recorded
from recorded import Recorder, recorder
from recorded import _recorder as _recorder_mod
from recorded._errors import JoinTimeoutError

# --- shared fixture: ensure each test starts with no module-level default ---


@pytest.fixture(autouse=True)
def _clean_default():
    """Clear `_default` before/after each test so `configure-once` semantics
    don't bleed across tests in this file."""
    _recorder_mod._set_default_for_testing(None)
    yield
    _recorder_mod._set_default_for_testing(None)


# --- configure() -----------------------------------------------------------


def test_configure_returns_recorder_with_given_path(tmp_path):
    """First `configure(path=p)` constructs a new singleton bound to `p`."""
    db = str(tmp_path / "jobs.db")
    r = recorded.configure(path=db)
    assert isinstance(r, Recorder)
    assert r.path == db
    # And it became the module default.
    assert _recorder_mod.get_default() is r


def test_configure_is_no_op_after_first_call(tmp_path):
    """Configure-once: a second `configure(...)` returns the existing
    default unchanged. Tests bypass via explicit `Recorder(...)` (or the
    `_set_default_for_testing` test hook used by other fixtures)."""
    db1 = str(tmp_path / "first.db")
    db2 = str(tmp_path / "second.db")
    r1 = recorded.configure(path=db1)
    r2 = recorded.configure(path=db2, join_timeout_s=999.0)
    assert r2 is r1
    # Args from the second call are silently ignored — no path swap, no
    # join_timeout swap.
    assert r1.path == db1
    assert r1.join_timeout_s != 999.0


# --- join_timeout_s plumbing ----------------------------------------------


@pytest.mark.asyncio
async def test_configure_sets_join_timeout_seen_by_handle_wait_default(tmp_path):
    """`JobHandle.wait(timeout=None)` reads `recorder.join_timeout_s` —
    not the module-constant fallback.

    Pins the configured-default propagation by waiting on a row that is
    never claimed (no leader, no executor). Cheaper and more direct than
    going through `.submit()` — that path's contract is exercised in
    `test_handle.py` and `test_cli_run.py`.
    """
    from recorded import JobHandle, _storage

    db = str(tmp_path / "jobs.db")
    r = recorded.configure(path=db, join_timeout_s=0.2)
    assert r.join_timeout_s == 0.2

    job_id = _storage.new_id()
    r._insert_pending(job_id, "t.cfg.never", None, _storage.now_iso(), None)
    h = JobHandle(job_id, r, "t.cfg.never")

    # No explicit timeout → uses the configured per-Recorder default of 0.2s.
    with pytest.raises(JoinTimeoutError) as excinfo:
        await h.wait()
    assert excinfo.value.timeout_s == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_configure_join_timeout_does_not_override_explicit_per_call_timeout(
    tmp_path,
):
    """Per-call `timeout=` always wins over the per-Recorder default."""
    from recorded import JobHandle, _storage

    db = str(tmp_path / "jobs.db")
    r = recorded.configure(path=db, join_timeout_s=60.0)
    assert r.join_timeout_s == 60.0

    job_id = _storage.new_id()
    r._insert_pending(job_id, "t.cfg.never2", None, _storage.now_iso(), None)
    h = JobHandle(job_id, r, "t.cfg.never2")

    # Explicit per-call timeout is 0.2s — the 60s Recorder default must not
    # win, otherwise this test would hang.
    with pytest.raises(JoinTimeoutError) as excinfo:
        await h.wait(timeout=0.2)
    assert excinfo.value.timeout_s == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_configure_join_timeout_reaches_idempotency_join_helpers(tmp_path):
    """The two idempotency-join helpers (`_async_wait_for_terminal`,
    `_wait_for_terminal_sync`) read `recorder.join_timeout_s` rather than
    the module constant.

    Pre-seeds a `running` row (no executor will terminate it) and
    triggers a same-key bare-call: it joins via `_async_try_join_existing`
    and the wait helper inherits the configured short timeout.
    """
    from recorded import _storage

    db = str(tmp_path / "jobs.db")
    r = recorded.configure(path=db, join_timeout_s=0.2)
    assert r.join_timeout_s == 0.2

    @recorder(kind="t.cfg.idem")
    async def slow(x):
        return {"x": x}

    # Pre-seed a `running` row that the bare call will join. No executor
    # claims it; the join's wait must respect the configured 0.2s timeout.
    blocker_id = _storage.new_id()
    now = _storage.now_iso()
    r._insert_running(blocker_id, "t.cfg.idem", "kk", now, now, '"x"')

    with pytest.raises(JoinTimeoutError) as excinfo:
        await slow(2, key="kk")
    assert excinfo.value.timeout_s == pytest.approx(0.2)


# --- async context manager -------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_async_context_manager_bootstraps_and_shuts_down(tmp_path):
    """`async with Recorder(...) as r` opens the connection (which runs the
    reaper as a side effect) and shuts down on exit."""
    db = str(tmp_path / "jobs.db")
    rec = Recorder(path=db)
    assert rec._conn is None

    async with rec as r:
        assert r is rec
        # Connection bootstrapped during __aenter__.
        assert r._conn is not None
        assert not r._closed

    # __aexit__ ran shutdown.
    assert rec._closed is True


# --- atexit lifecycle ------------------------------------------------------
#
# The dirty-recorder atexit warning machinery (`_LIVE_RECORDERS` +
# `_atexit_warn_dirty_recorders`) was introduced to surface the H-01
# hazard: `Recorder()` directly constructed, `.submit()` lazy-started a
# daemon worker, interpreter exited before `shutdown()`. Once the worker
# is gone (PLAN.md step 6), there is no daemon thread to leak — the hazard
# is structurally impossible — and the warning machinery goes with it.
# Tests pinning that warning have been removed.


def test_atexit_runs_shutdown_on_configured_default_only(tmp_path):
    """A subprocess that calls `recorded.configure(path=p)` and exits
    cleanly should leave no stale SQLite WAL/SHM lock files behind, and
    must exit 0. The *configured* default's `atexit.register(shutdown)`
    is what closes the connection.
    """
    db = tmp_path / "jobs.db"
    src = textwrap.dedent(
        f"""
        import recorded
        # Configure the default; do a tiny write so the connection actually
        # opens and creates -wal / -shm files.
        recorded.configure(path={str(db)!r})

        @recorded.recorder(kind="t.cfg.atexit")
        def fn(x):
            return {{"x": x}}

        fn(7)
        # Exit normally; atexit should close the connection cleanly.
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    assert proc.returncode == 0, (
        f"subprocess failed (code {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # Clean shutdown ⇒ WAL is checkpointed and the auxiliary files removed
    # by SQLite. Both -wal and -shm should be gone.
    assert db.exists(), "primary db file should exist"
    assert not (tmp_path / "jobs.db-wal").exists(), (
        "stale WAL file remains after subprocess exit; atexit shutdown probably did not run"
    )
    assert not (tmp_path / "jobs.db-shm").exists(), "stale SHM file remains after subprocess exit"
