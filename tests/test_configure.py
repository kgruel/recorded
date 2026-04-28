"""`recorded.configure(...)` + Recorder lifecycle additions (Stream C 2.C.1).

Configure-once semantics, `join_timeout_s` plumbing through both the
`JobHandle.wait()` default and the idempotency-join helpers, atexit
registration on the configured default, and the `async with Recorder(...)`
context manager wired for FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
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
    not the module-constant fallback."""
    db = str(tmp_path / "jobs.db")
    r = recorded.configure(path=db, join_timeout_s=0.2)
    assert r.join_timeout_s == 0.2

    @recorder(kind="t.cfg.never")
    async def never(x):
        await asyncio.sleep(60)

    h = never.submit(1)
    # No explicit timeout → uses the configured per-Recorder default of 0.2s.
    with pytest.raises(JoinTimeoutError) as excinfo:
        await h.wait()
    assert excinfo.value.timeout_s == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_configure_join_timeout_does_not_override_explicit_per_call_timeout(
    tmp_path,
):
    """Per-call `timeout=` always wins over the per-Recorder default."""
    db = str(tmp_path / "jobs.db")
    r = recorded.configure(path=db, join_timeout_s=60.0)
    assert r.join_timeout_s == 60.0

    @recorder(kind="t.cfg.never2")
    async def never(x):
        await asyncio.sleep(60)

    h = never.submit(1)
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

    Verified by holding a `pending` row alive (via worker-blocking sleep),
    then triggering an in-process keyed call collision: the second call's
    join helper inherits the configured short timeout and raises.
    """
    import threading

    db = str(tmp_path / "jobs.db")
    r = recorded.configure(path=db, join_timeout_s=0.2)
    assert r.join_timeout_s == 0.2

    started = threading.Event()
    proceed = threading.Event()

    @recorder(kind="t.cfg.idem")
    async def slow(x):
        started.set()
        await asyncio.to_thread(proceed.wait, 5.0)
        return {"x": x}

    h = slow.submit(1, key="kk")
    # Wait until the worker has the row in `running`.
    assert await asyncio.to_thread(started.wait, 5.0)

    # Idempotency-collision call: it sees the running row, subscribes,
    # and waits — but the wait should respect the configured 0.2s default.
    with pytest.raises(JoinTimeoutError) as excinfo:
        await slow(2, key="kk")
    assert excinfo.value.timeout_s == pytest.approx(0.2)

    # Cleanly drain so the worker thread joins fast.
    proceed.set()
    await h.wait(timeout=5.0)


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


def test_atexit_warns_when_direct_recorder_started_worker_but_never_shut_down(
    tmp_path,
):
    """Subprocess: build a Recorder directly, submit (which lazy-starts the
    worker), exit without `shutdown()`. The dirty-recorder atexit hook must
    log a RuntimeWarning-shaped message warning about the daemon-thread
    teardown hazard.
    """
    db = tmp_path / "jobs.db"
    src = textwrap.dedent(
        f"""
        import logging, sys
        # Surface "recorded" warnings on stderr so the parent test can see them.
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                            format="%(name)s %(levelname)s %(message)s")
        import recorded
        from recorded._recorder import Recorder
        import recorded._recorder as _rec

        r = Recorder(path={str(db)!r})
        _rec._set_default_for_testing(r)

        @recorded.recorder(kind="t.dirty.exit")
        def fn(x):
            return {{"x": x}}

        # Submit lazily starts the worker. Don't shutdown; exit dirty.
        h = fn.submit(1)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    # Subprocess may exit nonzero due to the asyncio teardown race — the
    # warning is what we care about.
    assert "constructed directly but never shut down" in proc.stderr, (
        f"expected dirty-recorder warning in stderr, got:\n{proc.stderr}"
    )


def test_atexit_does_not_warn_when_configure_is_used(tmp_path):
    """Configured singleton has `atexit.register(shutdown)` registered AFTER
    the dirty-recorder hook, so atexit-LIFO runs shutdown first. By the time
    the warning hook runs, `_closed=True` and the warning is suppressed.
    """
    db = tmp_path / "jobs.db"
    src = textwrap.dedent(
        f"""
        import logging, sys
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                            format="%(name)s %(levelname)s %(message)s")
        import recorded
        recorded.configure(path={str(db)!r})

        @recorded.recorder(kind="t.cfg.clean")
        def fn(x):
            return {{"x": x}}

        fn.submit(1)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert proc.returncode == 0, proc.stderr
    assert "never shut down" not in proc.stderr, (
        f"configure() singleton should not trigger dirty warning:\n"
        f"{proc.stderr}"
    )


def test_atexit_does_not_warn_when_with_block_used(tmp_path):
    """`with Recorder(...) as r:` calls shutdown via __exit__ before the
    interpreter exit hook runs — no warning."""
    db = tmp_path / "jobs.db"
    src = textwrap.dedent(
        f"""
        import logging, sys
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                            format="%(name)s %(levelname)s %(message)s")
        import recorded
        from recorded import Recorder
        import recorded._recorder as _rec

        with Recorder(path={str(db)!r}) as r:
            _rec._set_default_for_testing(r)

            @recorded.recorder(kind="t.with.clean")
            def fn(x):
                return {{"x": x}}

            fn.submit(1)
            # __exit__ on context manager calls shutdown before the atexit hook.
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert "never shut down" not in proc.stderr, (
        f"`with Recorder(...)` should not trigger dirty warning:\n"
        f"{proc.stderr}"
    )


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
        "stale WAL file remains after subprocess exit; "
        "atexit shutdown probably did not run"
    )
    assert not (tmp_path / "jobs.db-shm").exists(), (
        "stale SHM file remains after subprocess exit"
    )
