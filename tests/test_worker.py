"""Worker contract: lazy start, async + sync execution, atomic claim,
attach propagation, idempotent shutdown."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from recorded import JobHandle, Recorder, attach, recorder
from recorded import _storage


# ---- lazy start ----------------------------------------------------------


def test_worker_lazy_starts_on_first_submit(default_recorder):
    """Named test: no worker thread before `.submit()`; thread alive after.
    Bare calls do NOT spin up a worker."""

    @recorder(kind="t.worker.lazy")
    def f(x):
        return x

    initial_threads = {t.name for t in threading.enumerate()}
    assert "recorded-worker" not in initial_threads

    # Bare call — no worker.
    assert f(1) == 1
    assert "recorded-worker" not in {t.name for t in threading.enumerate()}

    # Submit — lazy-starts the worker.
    h = f.submit(2)
    assert isinstance(h, JobHandle)
    assert "recorded-worker" in {t.name for t in threading.enumerate()}

    # Drain so the test fixture's shutdown is fast.
    h.wait_sync(timeout=5.0)


# ---- async + sync execution on the same worker --------------------------


def test_worker_executes_async_and_sync_kinds_concurrently(default_recorder):
    """Named test: async fn and sync fn submitted to the same worker;
    both complete; lifecycle correct on both."""

    @recorder(kind="t.worker.async")
    async def aio_fn(x):
        await asyncio.sleep(0.01)
        return {"async_result": x}

    @recorder(kind="t.worker.sync")
    def sync_fn(x):
        time.sleep(0.01)
        return {"sync_result": x}

    h_a = aio_fn.submit(7)
    h_s = sync_fn.submit(11)

    job_a = h_a.wait_sync(timeout=5.0)
    job_s = h_s.wait_sync(timeout=5.0)

    assert job_a.status == "completed"
    assert job_a.response == {"async_result": 7}
    assert job_a.started_at is not None and job_a.completed_at is not None

    assert job_s.status == "completed"
    assert job_s.response == {"sync_result": 11}
    assert job_s.started_at is not None and job_s.completed_at is not None


# ---- attach inside a worker-executed coroutine --------------------------


def test_attach_propagates_to_worker_loop(default_recorder):
    """Named test: `attach()` inside a worker-executed coroutine writes
    to `data_json` correctly. ContextVar is set inside `_execute` before
    any await on the worker side."""

    @recorder(kind="t.worker.attach")
    async def fn(req):
        attach("k1", req["x"])
        attach("k2", "v2")
        return {"ok": True}

    h = fn.submit({"x": 99})
    job = h.wait_sync(timeout=5.0)
    assert job.status == "completed"
    assert job.data == {"k1": 99, "k2": "v2"}


def test_attach_propagates_to_worker_loop_for_sync_kind(default_recorder):
    """Same property for a sync fn dispatched via to_thread by the worker."""

    @recorder(kind="t.worker.attach.sync")
    def fn(req):
        attach("k", req)
        return {"echo": req}

    h = fn.submit("hello")
    job = h.wait_sync(timeout=5.0)
    assert job.data == {"k": "hello"}


# ---- atomic claim under concurrent submit load --------------------------


def test_atomic_claim_under_concurrent_submit_load(default_recorder):
    """Named test: submit 100 rows, observe each completes exactly once
    (no double-claim)."""

    counts: dict[int, int] = {}
    counts_lock = threading.Lock()

    @recorder(kind="t.worker.claim")
    def fn(x):
        with counts_lock:
            counts[x] = counts.get(x, 0) + 1
        return x

    handles = [fn.submit(i) for i in range(100)]
    jobs = [h.wait_sync(timeout=10.0) for h in handles]

    assert all(j.status == "completed" for j in jobs)
    assert sorted(j.response for j in jobs) == list(range(100))
    # Each input executed exactly once — atomic claim worked.
    assert all(counts[i] == 1 for i in range(100))
    assert sum(counts.values()) == 100


# ---- idempotent shutdown drains worker -----------------------------------


def test_recorder_shutdown_is_idempotent_and_drains_worker(db_path):
    """Named test: shutdown twice safely; worker thread joined; in-flight
    tasks resolve to terminal (cancelled or completed)."""

    rec = Recorder(path=db_path)
    from recorded import _recorder as _recorder_mod

    _recorder_mod._set_default(rec)

    proceed = threading.Event()

    @recorder(kind="t.worker.shutdown")
    def slow(x):
        # Wait for the test to release us (or until shutdown cancellation).
        if not proceed.wait(timeout=5.0):
            return x
        return x

    try:
        h1 = slow.submit(1)
        # Give worker a chance to claim+start.
        time.sleep(0.05)
        # Shut down WHILE in-flight. Should cancel and join.
        rec.shutdown()
        rec.shutdown()  # idempotent
    finally:
        proceed.set()
        _recorder_mod._set_default(None)

    # Worker thread is gone after shutdown.
    assert "recorded-worker" not in {t.name for t in threading.enumerate()}

    # The in-flight row should be terminal — not stuck `running`. Reopen
    # a fresh Recorder against the same DB to inspect.
    rec2 = Recorder(path=db_path)
    try:
        job = rec2.get(h1.job_id)
        assert job is not None
        assert job.status in (_storage.STATUS_FAILED, _storage.STATUS_COMPLETED)
    finally:
        rec2.shutdown()


# ---- wait_sync inside a running loop raises -----------------------------


@pytest.mark.asyncio
async def test_handle_wait_sync_inside_loop_raises_helpful_error(
    default_recorder,
):
    """Named test: `.wait_sync()` from inside a running loop raises
    `SyncInLoopError` (matches `.sync()` precedent)."""
    from recorded._errors import SyncInLoopError

    @recorder(kind="t.worker.sync_in_loop")
    async def fn(x):
        return x

    h = fn.submit(1)
    with pytest.raises(SyncInLoopError, match="running event loop"):
        h.wait_sync()
    # Drain via async path so the worker can complete cleanly.
    await h.wait(timeout=5.0)


# ---- end-to-end submit + wait -------------------------------------------


@pytest.mark.asyncio
async def test_submit_returns_handle_whose_wait_resolves_to_terminal_job(
    default_recorder,
):
    """Named test: end-to-end submit + async wait."""

    @recorder(kind="t.worker.e2e")
    async def fn(x):
        await asyncio.sleep(0.01)
        return {"x": x, "ok": True}

    h = fn.submit(5)
    assert isinstance(h, JobHandle)
    job = await h.wait(timeout=5.0)
    assert job.status == "completed"
    assert job.response == {"x": 5, "ok": True}
    assert job.id == h.job_id
