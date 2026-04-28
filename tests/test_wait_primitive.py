"""Wait-primitive contract: `Recorder._subscribe` / `_resolve`.

These futures are the wakeup mechanism that `JobHandle.wait()`,
`.wait_sync()`, and the in-process idempotency-collision joiners share.
The tests target the primitive directly (no decorator) so the contract
is pinned independently of how the surfaces consume it.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from recorded import Recorder, _storage, recorder


def test_subscribe_resolves_when_terminal_write_commits(recorder: Recorder):
    """Named test: register a Future before the terminal write; the
    terminal SQL UPDATE resolves it exactly once with the terminal status."""
    job_id = _storage.new_id()
    now = _storage.now_iso()
    recorder._insert_running(job_id, "t.subscribe", None, now, now, None)

    fut = recorder._subscribe(job_id)
    assert not fut.done()

    def writer() -> None:
        recorder._mark_completed(job_id, _storage.now_iso(), '{"x": 1}', None)

    t = threading.Thread(target=writer)
    t.start()
    status = fut.result(timeout=5.0)
    t.join()

    assert status == _storage.STATUS_COMPLETED
    assert job_id not in recorder._notify_subscribers


def test_subscribe_returns_immediately_if_already_terminal(
    recorder: Recorder,
):
    """Named test: subscribing after the terminal write doesn't deadlock;
    the future is pre-resolved before `_subscribe` returns."""
    job_id = _storage.new_id()
    now = _storage.now_iso()
    recorder._insert_running(job_id, "t.subscribe.after", None, now, now, None)
    recorder._mark_completed(job_id, _storage.now_iso(), "{}", None)

    fut = recorder._subscribe(job_id)
    assert fut.done()
    assert fut.result(timeout=0) == _storage.STATUS_COMPLETED


def test_resolve_only_fires_when_conditional_update_matches(
    recorder: Recorder,
):
    """A late completion against a row already terminated by another writer
    (e.g. the reaper) is silently dropped — the late `_mark_completed` /
    `_mark_failed` doesn't re-resolve subscribers."""
    job_id = _storage.new_id()
    now = _storage.now_iso()
    recorder._insert_running(job_id, "t.resolve.gate", None, now, now, None)
    recorder._mark_failed(job_id, _storage.now_iso(), '{"type":"X"}')

    # Late completion: conditional UPDATE doesn't match (status != running).
    # No second resolve should fire; row stays failed.
    recorder._mark_completed(job_id, _storage.now_iso(), "{}", None)
    assert recorder._row_status(job_id) == _storage.STATUS_FAILED


def test_idempotency_join_in_process_does_not_poll_with_sleep(default_recorder, monkeypatch):
    """Named test (`test_idempotency_join_uses_notify_not_polling`): the
    in-process idempotency-collision join wakes via the notify primitive,
    not via `time.sleep` polling.

    Strategy: the joining call is dispatched to a background thread which
    blocks on `_subscribe()`'s Future; the writer thread is the test
    itself, calling `_mark_completed` once the joiner is parked. The
    `time.sleep` spy must record zero calls inside the join helper.
    """

    sleep_calls: list[float] = []
    real_sleep = time.sleep

    def tracking_sleep(s: float, *a, **k) -> None:
        sleep_calls.append(s)
        real_sleep(s, *a, **k)

    @recorder(kind="t.notify.sync.unify")
    def fn(x):
        return {"x": x}

    # Pre-seed a pending row that the joiner will wait on.
    rec = default_recorder
    job_id = _storage.new_id()
    now = _storage.now_iso()
    rec._insert_running(job_id, "t.notify.sync.unify", "kk", now, now, None)

    join_result: dict = {}

    # Fires on the joiner thread once it enters `_subscribe`. `Event.set()`
    # is sleep-free, so the join-path-must-not-sleep assertion is preserved.
    parked = threading.Event()

    def on_subscribe(jid: str) -> None:
        if jid == job_id:
            parked.set()

    monkeypatch.setattr(rec, "_for_testing_subscribe_callback", on_subscribe)

    def joiner() -> None:
        # Patch *inside* the joiner thread so we only count sleeps that
        # happen on the join path (not test-harness overhead like the
        # outer `t.join(timeout=)` which uses Event.wait, not time.sleep).
        monkeypatch.setattr(time, "sleep", tracking_sleep)
        try:
            join_result["value"] = fn(1, key="kk")
        finally:
            monkeypatch.setattr(time, "sleep", real_sleep)

    t = threading.Thread(target=joiner)
    t.start()
    # Joiner has parked on `_subscribe`; safe to write terminal.
    assert parked.wait(timeout=5.0)
    rec._mark_completed(job_id, _storage.now_iso(), '{"x": 1}', None)
    t.join(timeout=5.0)

    assert join_result["value"] == {"x": 1}
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_idempotency_join_uses_notify_not_polling(default_recorder, monkeypatch):
    """Async counterpart: the async join helper wakes via the notify
    primitive without calling `asyncio.sleep` on the in-process path."""

    sleep_calls: list[float] = []
    import asyncio as aio_mod

    real_async_sleep = aio_mod.sleep

    async def tracking_async_sleep(s: float, *a, **k) -> None:
        sleep_calls.append(s)
        await real_async_sleep(s, *a, **k)

    started = asyncio.Event()
    proceed = asyncio.Event()

    @recorder(kind="t.notify.async.unify")
    async def slow():
        started.set()
        await proceed.wait()
        return {"ok": True}

    a_task = asyncio.create_task(slow(key="kk2"))
    await started.wait()

    # Fires once B parks on `_subscribe`. `Event.set` via call_soon is
    # sleep-free, preserving the "join path must not sleep" invariant.
    subscribed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_subscribe(_jid: str) -> None:
        loop.call_soon_threadsafe(subscribed.set)

    monkeypatch.setattr(default_recorder, "_for_testing_subscribe_callback", on_subscribe)

    # Patch only after the leader is past its own internal awaits.
    monkeypatch.setattr(aio_mod, "sleep", tracking_async_sleep)
    try:
        b_task = asyncio.create_task(slow(key="kk2"))
        # B has reached `_async_wait_for_terminal` and subscribed.
        await subscribed.wait()
        proceed.set()
        a, b = await asyncio.gather(a_task, b_task)
    finally:
        monkeypatch.setattr(aio_mod, "sleep", real_async_sleep)

    assert a == b == {"ok": True}
    assert sleep_calls == []
