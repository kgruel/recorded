"""`JobHandle` contract: idempotency-collision behavior and the
wrap-transparency rule for `.wait()` failure paths."""

from __future__ import annotations

import asyncio

import pytest

from recorded import JobHandle, recorder
from recorded._errors import (
    JoinedSiblingFailedError,
    JoinTimeoutError,
    RowDisappearedError,
)


@pytest.mark.asyncio
async def test_submit_idempotency_collision_returns_handle_to_existing_active_row(
    default_recorder,
):
    """Named test: `submit(key='x')` twice while pending → both handles
    resolve to the same `Job`.

    Synchronization uses `threading.Event` because the worker runs on a
    dedicated thread + event loop separate from the test's loop —
    `asyncio.Event` is bound to a single loop and would not signal
    across them.
    """
    import threading

    started = threading.Event()
    proceed = threading.Event()
    runs = 0

    @recorder(kind="t.handle.idem")
    async def slow(x):
        nonlocal runs
        runs += 1
        started.set()
        # Wait on a thread primitive from the worker's coroutine.
        await asyncio.to_thread(proceed.wait)
        return {"x": x, "runs": runs}

    h1 = slow.submit(1, key="kk")
    # Wait until the worker has picked it up so the second submit collides
    # while the row is `pending`-or-`running`.
    await asyncio.to_thread(started.wait, 5.0)
    assert started.is_set()

    h2 = slow.submit(2, key="kk")

    # Both handles point at the same row.
    assert h1.job_id == h2.job_id

    proceed.set()
    job1 = await h1.wait(timeout=5.0)
    job2 = await h2.wait(timeout=5.0)
    assert job1.id == job2.id
    assert job1.response == job2.response
    assert runs == 1


@pytest.mark.asyncio
async def test_submit_idempotency_collision_with_retry_failed_false_raises_on_wait(
    default_recorder,
):
    """Named test: failed row + `retry_failed=False` + `submit().wait()`
    → raises `JoinedSiblingFailedError` (per wrap-transparency: keyed
    calls never return a `Job` for failure paths)."""

    @recorder(kind="t.handle.no_retry")
    async def flaky(x):
        raise ValueError("boom")

    # First submission fails. `.wait()` always raises on failure (per
    # wrap-transparency); the wrapped function's exception is captured in
    # `sibling_error`, not re-raised verbatim from the handle.
    h1 = flaky.submit(1, key="kk")
    with pytest.raises(JoinedSiblingFailedError) as excinfo_first:
        await h1.wait(timeout=5.0)
    assert excinfo_first.value.sibling_error == {
        "type": "ValueError",
        "message": "boom",
    }

    # Second submission with retry_failed=False resolves to the prior
    # failed row's handle. wait() raises rather than returning the Job.
    h2 = flaky.submit(2, key="kk", retry_failed=False)
    assert isinstance(h2, JobHandle)
    with pytest.raises(JoinedSiblingFailedError) as excinfo:
        await h2.wait(timeout=5.0)
    assert excinfo.value.sibling_error == {
        "type": "ValueError",
        "message": "boom",
    }


@pytest.mark.asyncio
async def test_handle_wait_timeout_raises_join_timeout(default_recorder):
    """A handle whose row never reaches terminal raises `JoinTimeoutError`."""
    from recorded import _storage

    rec = default_recorder
    # Insert a pending row directly — no worker will ever execute it
    # because there's no registered kind.
    job_id = _storage.new_id()
    rec._insert_pending(job_id, "t.handle.never", None, _storage.now_iso(), None)

    h = JobHandle(job_id, rec, "t.handle.never")
    with pytest.raises(JoinTimeoutError) as excinfo:
        await h.wait(timeout=0.3)
    assert excinfo.value.sibling_job_id == job_id


@pytest.mark.asyncio
async def test_handle_wait_raises_row_disappeared_when_row_deleted(
    default_recorder,
):
    """A waiter parked on a row that gets deleted out from under it must
    fail-fast with `RowDisappearedError` rather than wait until
    `JoinTimeoutError` (default 30 s) elapses."""
    from recorded import _storage

    rec = default_recorder
    job_id = _storage.new_id()
    # Insert a pending row with no registered kind — no worker will ever
    # claim it; we control its lifecycle directly.
    rec._insert_pending(job_id, "t.handle.disappear", None, _storage.now_iso(), None)

    h = JobHandle(job_id, rec, "t.handle.disappear")

    # Schedule a deletion shortly after wait() parks.
    async def _delete_after_park():
        await asyncio.sleep(0.05)
        await asyncio.to_thread(
            lambda: rec.connection().execute(
                "DELETE FROM jobs WHERE id = ?", (job_id,)
            ),
        )

    deleter = asyncio.create_task(_delete_after_park())
    try:
        # Use a generous timeout — the test asserts we fail fast, not via
        # the timeout path.
        with pytest.raises(RowDisappearedError) as excinfo:
            await h.wait(timeout=5.0)
        assert excinfo.value.sibling_job_id == job_id
    finally:
        await deleter


def test_handle_wait_sync_raises_row_disappeared_when_row_deleted(
    default_recorder,
):
    """Sync variant of the disappear-fail-fast contract."""
    import threading

    from recorded import _storage

    rec = default_recorder
    job_id = _storage.new_id()
    rec._insert_pending(job_id, "t.handle.disappear_sync", None, _storage.now_iso(), None)

    h = JobHandle(job_id, rec, "t.handle.disappear_sync")

    # Delete the row from another thread shortly after wait_sync parks.
    def _delete_after_park():
        import time
        time.sleep(0.05)
        rec.connection().execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    t = threading.Thread(target=_delete_after_park, daemon=True)
    t.start()
    try:
        with pytest.raises(RowDisappearedError) as excinfo:
            h.wait_sync(timeout=5.0)
        assert excinfo.value.sibling_job_id == job_id
    finally:
        t.join(timeout=2.0)


def test_handle_wait_sync_returns_terminal_job(default_recorder):
    """`wait_sync()` blocks the calling thread, returns Job on success."""

    @recorder(kind="t.handle.sync_wait")
    def fn(x):
        return {"x": x}

    h = fn.submit(3)
    job = h.wait_sync(timeout=5.0)
    assert job.status == "completed"
    assert job.response == {"x": 3}


def test_handle_wait_sync_failure_raises_joined_sibling_failed(default_recorder):
    """`wait_sync()` raises `JoinedSiblingFailedError` for failed terminal."""

    @recorder(kind="t.handle.sync_fail")
    def fn(x):
        raise RuntimeError("nope")

    h = fn.submit(1)
    with pytest.raises(JoinedSiblingFailedError) as excinfo:
        h.wait_sync(timeout=5.0)
    assert excinfo.value.sibling_error == {
        "type": "RuntimeError",
        "message": "nope",
    }
