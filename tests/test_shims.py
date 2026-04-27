"""Cross-mode shims: `.sync` (sync caller, async fn) and `.async_run`."""

from __future__ import annotations

import asyncio

import pytest

from recorded import recorder
from recorded._errors import SyncInLoopError


def test_sync_shim_runs_async_function_from_sync_caller(default_recorder):
    @recorder(kind="t.shim.sync")
    async def afn(x):
        await asyncio.sleep(0)
        return x + 1

    assert afn.sync(41) == 42

    status = (
        default_recorder._connection()
        .execute("SELECT status FROM jobs WHERE kind=?", ("t.shim.sync",))
        .fetchone()[0]
    )
    assert status == "completed"


@pytest.mark.asyncio
async def test_sync_call_inside_running_loop_raises_helpful_error(default_recorder):
    """Named test: calling `.sync()` from inside an event loop raises
    `SyncInLoopError` with guidance pointing at the bare await form."""

    @recorder(kind="t.shim.bad_sync")
    async def afn(x):
        return x

    with pytest.raises(SyncInLoopError, match=r"running event loop"):
        afn.sync(1)


@pytest.mark.asyncio
async def test_async_run_shim_runs_sync_function_from_async_caller(default_recorder):
    @recorder(kind="t.shim.async_run")
    def sfn(x):
        return x * 2

    out = await sfn.async_run(21)
    assert out == 42


@pytest.mark.asyncio
async def test_async_run_propagates_attach_via_to_thread(default_recorder):
    """contextvars propagate through `asyncio.to_thread`, so attach() works
    inside a sync function called via `.async_run`."""
    from recorded import attach
    import json

    @recorder(kind="t.shim.attach")
    def sfn():
        attach("hello", "world")
        return {"ok": True}

    await sfn.async_run()
    raw = (
        default_recorder._connection()
        .execute("SELECT data_json FROM jobs WHERE kind=?", ("t.shim.attach",))
        .fetchone()[0]
    )
    assert json.loads(raw) == {"hello": "world"}
