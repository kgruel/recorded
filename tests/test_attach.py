"""attach() contextvar plumbing + end-to-end buffer/flush behaviors."""

from __future__ import annotations

import asyncio
import json

import pytest

from recorded import attach, recorder
from recorded._context import JobContext, current_job
from recorded._errors import AttachOutsideJobError


def test_attach_outside_job_raises():
    with pytest.raises(AttachOutsideJobError):
        attach("k", "v")


def test_attach_buffers_into_current_context():
    ctx = JobContext(job_id="j", kind="k", recorder=None)
    token = current_job.set(ctx)
    try:
        attach("a", 1)
        attach("b", "two")
    finally:
        current_job.reset(token)
    assert ctx.buffer == {"a": 1, "b": "two"}


def test_contextvar_isolated_across_asyncio_gather():
    """Each task gets its own context copy — buffers don't cross-contaminate."""

    async def in_task(name: str) -> dict:
        ctx = JobContext(job_id=name, kind="k", recorder=None)
        token = current_job.set(ctx)
        try:
            attach("name", name)
            await asyncio.sleep(0)
            attach("after_sleep", name)
            return ctx.buffer
        finally:
            current_job.reset(token)

    async def main():
        return await asyncio.gather(in_task("a"), in_task("b"), in_task("c"))

    a, b, c = asyncio.run(main())
    assert a == {"name": "a", "after_sleep": "a"}
    assert b == {"name": "b", "after_sleep": "b"}
    assert c == {"name": "c", "after_sleep": "c"}


# --- end-to-end with the decorator ----------------------------------------


@pytest.mark.asyncio
async def test_attach_buffers_until_completion_by_default(default_recorder):
    """Named test: attach() does not write to the row mid-execution; it
    flushes once at completion.

    We assert this by sampling `data_json` from inside the wrapped
    function (before completion) and again after it returns.
    """

    snapshots: list[str | None] = []

    @recorder(kind="t.buffer")
    async def fn():
        attach("a", 1)
        attach("b", "two")
        # data_json is still NULL at this point — nothing has been written.
        snapshots.append(
            default_recorder._connection()
            .execute("SELECT data_json FROM jobs WHERE kind=?", ("t.buffer",))
            .fetchone()[0]
        )
        return {"ok": True}

    await fn()

    # Pre-completion snapshot: nothing flushed yet.
    assert snapshots == [None]

    final = (
        default_recorder._connection()
        .execute("SELECT data_json FROM jobs WHERE kind=?", ("t.buffer",))
        .fetchone()[0]
    )
    assert json.loads(final) == {"a": 1, "b": "two"}


@pytest.mark.asyncio
async def test_attach_flush_true_writes_through(default_recorder):
    """Named test: `attach(..., flush=True)` lands in `data_json` immediately."""

    captured: dict[str, object] = {}

    @recorder(kind="t.flush")
    async def fn():
        attach("immediate", "yes", flush=True)
        captured["mid"] = (
            default_recorder._connection()
            .execute("SELECT data_json FROM jobs WHERE kind=?", ("t.flush",))
            .fetchone()[0]
        )
        attach("buffered", 42)  # default — not written until completion
        captured["after_buffered"] = (
            default_recorder._connection()
            .execute("SELECT data_json FROM jobs WHERE kind=?", ("t.flush",))
            .fetchone()[0]
        )
        return None

    await fn()

    # Mid-call: only the flushed key visible.
    assert json.loads(captured["mid"]) == {"immediate": "yes"}
    # The buffered attach is still in memory only.
    assert json.loads(captured["after_buffered"]) == {"immediate": "yes"}

    # After completion: both keys present (buffered merged on top).
    final = (
        default_recorder._connection()
        .execute("SELECT data_json FROM jobs WHERE kind=?", ("t.flush",))
        .fetchone()[0]
    )
    assert json.loads(final) == {"immediate": "yes", "buffered": 42}


@pytest.mark.asyncio
async def test_async_attach_isolated_across_gather(default_recorder):
    """Named test: concurrent recorded coroutines have independent attach
    buffers under asyncio.gather — keys do not leak across tasks."""

    @recorder(kind="t.gather")
    async def fn(name: str):
        attach("name", name)
        # Yield repeatedly so the tasks interleave.
        for _ in range(5):
            await asyncio.sleep(0)
        attach("done", name)
        return {"name": name}

    results = await asyncio.gather(fn("a"), fn("b"), fn("c"))
    assert {r["name"] for r in results} == {"a", "b", "c"}

    rows = (
        default_recorder._connection()
        .execute(
            "SELECT data_json FROM jobs WHERE kind=? ORDER BY submitted_at",
            ("t.gather",),
        )
        .fetchall()
    )
    payloads = [json.loads(r[0]) for r in rows]
    by_name = {p["name"]: p for p in payloads}

    # Each row's data has *only* its own name in both keys — no leakage.
    for name in ("a", "b", "c"):
        assert by_name[name] == {"name": name, "done": name}


@pytest.mark.asyncio
async def test_attach_overrides_data_projection_last_write_wins(default_recorder):
    """attach() merges over a `data=` projection of the response."""
    from dataclasses import dataclass

    @dataclass
    class View:
        a: int
        b: str

    @recorder(kind="t.proj", data=View)
    async def fn():
        attach("a", 999)  # overrides the projected `a`
        return {"a": 1, "b": "hi", "extra": "ignored"}

    await fn()
    raw = (
        default_recorder._connection()
        .execute("SELECT data_json FROM jobs WHERE kind=?", ("t.proj",))
        .fetchone()[0]
    )
    assert json.loads(raw) == {"a": 999, "b": "hi"}
