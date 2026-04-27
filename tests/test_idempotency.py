"""Idempotency at the call site, plus the auto-kind-+-key validation."""

from __future__ import annotations

import asyncio

import pytest

from recorded import recorder
from recorded._errors import ConfigurationError


# --- call-time validation of key= against auto-derived kind ---------------


def test_key_with_auto_kind_raises_when_key_is_used(default_recorder):
    """Named test: a function decorated with `@recorder` (auto-derived kind)
    refuses to accept `key=` at the call site.

    Validation happens at call time, not decoration time — we can't know
    at decoration whether the caller will use `key=` (and refusing every
    auto-kind decoration would defeat the convenience). The error message
    names the auto-derived kind and points the user at the explicit-kind fix.
    """

    @recorder
    def f(x):
        return x

    with pytest.raises(ConfigurationError, match=r"auto-derived kind"):
        f(1, key="any-key")


@pytest.mark.asyncio
async def test_key_with_auto_kind_raises_for_async_too(default_recorder):
    @recorder
    async def f(x):
        return x

    with pytest.raises(ConfigurationError, match=r"auto-derived kind"):
        await f(1, key="any-key")


def test_explicit_kind_permits_key(default_recorder):
    @recorder(kind="t.explicit")
    def f(x):
        return x

    # Should not raise; first call writes a row.
    assert f(1, key="k1") == 1


# --- collision: completed → return without re-execution -------------------


@pytest.mark.asyncio
async def test_idempotency_collision_returns_existing_completed_without_reexecution(
    default_recorder,
):
    """Named test."""
    calls: list[int] = []

    @recorder(kind="t.idem.completed")
    async def place(x):
        calls.append(x)
        return {"ok": True, "x": x}

    a = await place(1, key="abc")
    b = await place(2, key="abc")  # same key — must NOT re-execute

    assert a == {"ok": True, "x": 1}
    assert b == {"ok": True, "x": 1}  # the *original* response
    assert calls == [1]

    # Only one row.
    count = (
        default_recorder._connection()
        .execute(
            "SELECT count(*) FROM jobs WHERE kind=? AND key=?",
            ("t.idem.completed", "abc"),
        )
        .fetchone()[0]
    )
    assert count == 1


# --- collision on pending/running: race two callers via gather ------------


@pytest.mark.asyncio
async def test_idempotency_collision_on_pending_waits_for_terminal(default_recorder):
    """Named test: two callers race the same `key`. Exactly one executes;
    the other observes the active row and joins on its terminal status.

    Phase-1 has no worker, so we drive this with `asyncio.gather` —
    one caller wins the INSERT, the other catches the unique-index
    collision and sync-polls the row to terminal."""

    started = asyncio.Event()
    proceed = asyncio.Event()
    runs = 0

    @recorder(kind="t.idem.race")
    async def slow():
        nonlocal runs
        runs += 1
        started.set()
        # Hold in `running` until both callers have collided.
        await proceed.wait()
        return {"who": "winner", "runs": runs}

    async def caller():
        return await slow(key="race-1")

    a_task = asyncio.create_task(caller())
    # Wait until A has reached `running`. B will collide on INSERT and poll.
    await started.wait()
    b_task = asyncio.create_task(caller())
    # Give B time to collide and start polling.
    await asyncio.sleep(0.02)
    proceed.set()

    a, b = await asyncio.gather(a_task, b_task)

    assert runs == 1
    assert a == b == {"who": "winner", "runs": 1}


# --- failed: retry by default --------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_with_failed_retries_by_default(default_recorder):
    """Named test."""
    n = 0

    @recorder(kind="t.idem.retry")
    async def flaky():
        nonlocal n
        n += 1
        if n == 1:
            raise RuntimeError("first attempt fails")
        return {"attempt": n}

    with pytest.raises(RuntimeError, match="first attempt"):
        await flaky(key="k")
    # Default: retry on failure → new row is inserted, executes again.
    out = await flaky(key="k")
    assert out == {"attempt": 2}

    rows = (
        default_recorder._connection()
        .execute(
            "SELECT status FROM jobs WHERE kind=? AND key=? ORDER BY submitted_at",
            ("t.idem.retry", "k"),
        )
        .fetchall()
    )
    assert [r[0] for r in rows] == ["failed", "completed"]


# --- failed + retry_failed=False: return existing failed Job --------------


@pytest.mark.asyncio
async def test_idempotency_retry_failed_false_returns_failed_job(default_recorder):
    """Named test: with `retry_failed=False`, a prior failure is returned
    as a Job object rather than retried."""
    n = 0

    @recorder(kind="t.idem.no_retry")
    async def flaky():
        nonlocal n
        n += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await flaky(key="k")

    # Second call, retry_failed=False → returns the prior failed Job.
    job = await flaky(key="k", retry_failed=False)

    # Returned value is a Job (not the response), since there's no response.
    from recorded import Job
    assert isinstance(job, Job)
    assert job.status == "failed"
    assert job.error == {"type": "ValueError", "message": "boom"}
    # And no second execution happened.
    assert n == 1
