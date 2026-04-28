"""`JobHandle` contract: idempotency-collision behavior and the
wrap-transparency rule for `.wait()` failure paths.

Tests that exercise `.submit()` end-to-end use the `leader_recorder`
fixture (which spawns a `python -m recorded run` subprocess) and
canonical kinds from `tests/_leader_kinds.py`. Tests that manipulate
rows directly (timeout, RowDisappeared, no .submit()) use
`default_recorder` — they don't need a leader.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from recorded import JobHandle, _storage
from recorded._errors import (
    JoinedSiblingFailedError,
    JoinTimeoutError,
    RowDisappearedError,
)

# `_leader_kinds` is loaded by the `leader_recorder` fixture (which adds
# `tests/` to sys.path). Resolve it here for tests that use the fixture so
# the import is visible at decoration-time.
sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _leader_kinds  # noqa: E402, PLC0415

sys.path.pop(0)


@pytest.mark.asyncio
async def test_submit_idempotency_collision_returns_handle_to_existing_active_row(
    leader_recorder,
):
    """Named test: `submit(key='x')` twice while pending → both handles
    resolve to the same `Job`.

    Cross-process leader: in-process events can't synchronize with the
    leader subprocess. Use a slow function (sleeps in the leader) so the
    second `.submit()` lands while the first row is still pending or
    running. `runs == 1` is asserted post-hoc by counting actual rows
    with the keyed lookup.
    """
    h1 = _leader_kinds.slow_echo.submit({"sleep": 0.5, "value": 1}, key="kk-collision")
    # No event-based synchronization across the process boundary —
    # immediately submit again with the same key. The pre-INSERT
    # idempotency lookup (or the IntegrityError fallback) folds h2 into
    # the same row as h1.
    h2 = _leader_kinds.slow_echo.submit({"sleep": 0.5, "value": 2}, key="kk-collision")

    # Both handles point at the same row.
    assert h1.job_id == h2.job_id

    job1 = await h1.wait(timeout=5.0)
    job2 = await h2.wait(timeout=5.0)
    assert job1.id == job2.id
    assert job1.response == job2.response

    # Exactly one row exists for that key — proof that idempotency held.
    rec = leader_recorder
    rows = rec._fetchall(
        "SELECT id FROM jobs WHERE kind=? AND key=?",
        ("t.leader.slow_echo", "kk-collision"),
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_submit_idempotency_collision_with_retry_failed_false_raises_on_wait(
    leader_recorder,
):
    """Named test: failed row + `retry_failed=False` + `submit().wait()`
    → raises `JoinedSiblingFailedError` (per wrap-transparency: keyed
    calls never return a `Job` for failure paths)."""
    # First submission fails. `.wait()` always raises on failure (per
    # wrap-transparency); the wrapped function's exception is captured in
    # `sibling_error`, not re-raised verbatim from the handle.
    h1 = _leader_kinds.fails.submit("first", key="kk-no-retry")
    with pytest.raises(JoinedSiblingFailedError) as excinfo_first:
        await h1.wait(timeout=5.0)
    assert excinfo_first.value.sibling_error == {
        "type": "RuntimeError",
        "message": "intentional failure: 'first'",
    }

    # Second submission with retry_failed=False resolves to the prior
    # failed row's handle. wait() raises rather than returning the Job.
    h2 = _leader_kinds.fails.submit("second", key="kk-no-retry", retry_failed=False)
    assert isinstance(h2, JobHandle)
    with pytest.raises(JoinedSiblingFailedError) as excinfo:
        await h2.wait(timeout=5.0)
    assert excinfo.value.sibling_error == {
        "type": "RuntimeError",
        "message": "intentional failure: 'first'",
    }


@pytest.mark.asyncio
async def test_handle_wait_timeout_raises_join_timeout(default_recorder):
    """A handle whose row never reaches terminal raises `JoinTimeoutError`."""

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
async def test_handle_wait_raises_row_disappeared_when_row_deleted(default_recorder, monkeypatch):
    """A waiter parked on a row that gets deleted out from under it must
    fail-fast with `RowDisappearedError` rather than wait until
    `JoinTimeoutError` (default 30 s) elapses."""

    rec = default_recorder
    job_id = _storage.new_id()
    # Insert a pending row with no registered kind — no worker will ever
    # claim it; we control its lifecycle directly.
    rec._insert_pending(job_id, "t.handle.disappear", None, _storage.now_iso(), None)

    h = JobHandle(job_id, rec, "t.handle.disappear")

    # Fire the deletion exactly once `_subscribe` has registered our
    # future — the precondition `RowDisappearedError` requires.
    parked = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_subscribe(jid: str) -> None:
        if jid == job_id:
            loop.call_soon_threadsafe(parked.set)

    monkeypatch.setattr(rec, "_for_testing_subscribe_callback", on_subscribe)

    async def _delete_after_park():
        await parked.wait()
        await asyncio.to_thread(
            lambda: rec.connection().execute("DELETE FROM jobs WHERE id = ?", (job_id,)),
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


def test_handle_wait_sync_raises_row_disappeared_when_row_deleted(default_recorder, monkeypatch):
    """Sync variant of the disappear-fail-fast contract."""
    import threading

    rec = default_recorder
    job_id = _storage.new_id()
    rec._insert_pending(job_id, "t.handle.disappear_sync", None, _storage.now_iso(), None)

    h = JobHandle(job_id, rec, "t.handle.disappear_sync")

    # Delete the row once `wait_sync()` has parked on `_subscribe`.
    parked = threading.Event()

    def on_subscribe(jid: str) -> None:
        if jid == job_id:
            parked.set()

    monkeypatch.setattr(rec, "_for_testing_subscribe_callback", on_subscribe)

    def _delete_after_park():
        parked.wait(timeout=5.0)
        rec.connection().execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    t = threading.Thread(target=_delete_after_park, daemon=True)
    t.start()
    try:
        with pytest.raises(RowDisappearedError) as excinfo:
            h.wait_sync(timeout=5.0)
        assert excinfo.value.sibling_job_id == job_id
    finally:
        t.join(timeout=2.0)


def test_handle_wait_sync_returns_terminal_job(leader_recorder):
    """`wait_sync()` blocks the calling thread, returns Job on success."""
    h = _leader_kinds.echo.submit(3)
    job = h.wait_sync(timeout=5.0)
    assert job.status == "completed"
    assert job.response == {"echoed": 3}


def test_handle_wait_sync_failure_raises_joined_sibling_failed(leader_recorder):
    """`wait_sync()` raises `JoinedSiblingFailedError` for failed terminal."""
    h = _leader_kinds.fails.submit("x", key="kk-sync-fail")
    with pytest.raises(JoinedSiblingFailedError) as excinfo:
        h.wait_sync(timeout=5.0)
    assert excinfo.value.sibling_error == {
        "type": "RuntimeError",
        "message": "intentional failure: 'x'",
    }
