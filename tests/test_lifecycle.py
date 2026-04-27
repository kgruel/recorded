"""Bare-call lifecycle: pending -> running -> terminal, sync and async."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from recorded import recorder
from recorded import _storage


# --- shared helpers --------------------------------------------------------


def _events_for(default_recorder, job_id: str) -> list[tuple[str, ...]]:
    """Reconstruct the lifecycle observed for a given row.

    We can't watch transitions in flight (we'd need a trigger), but we
    can prove the *terminal* state has each timestamp set in the right
    monotonic order. Combined with the explicit row-status snapshot
    below, this is sufficient to exercise the contract.
    """
    row = default_recorder._connection().execute(
        "SELECT status, submitted_at, started_at, completed_at "
        "FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    return row


# --- async lifecycle -------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_call_writes_pending_running_terminal_in_order(default_recorder):
    """Named test: a bare call lands as completed, with all three timestamps
    set in monotonic order — i.e., it actually went through pending then
    running before reaching terminal."""

    seen_during_call: list[str] = []

    @recorder(kind="t.async")
    async def do(x):
        # While we're inside the wrapped function, the row should be running.
        rows = default_recorder._connection().execute(
            "SELECT status FROM jobs WHERE kind=?", ("t.async",)
        ).fetchall()
        seen_during_call.extend(r[0] for r in rows)
        return {"x": x, "ok": True}

    result = await do(7)

    assert result == {"x": 7, "ok": True}
    assert seen_during_call == ["running"]

    rows = default_recorder._connection().execute(
        "SELECT status, submitted_at, started_at, completed_at, response_json "
        "FROM jobs WHERE kind=?",
        ("t.async",),
    ).fetchall()
    assert len(rows) == 1
    status, sub, start, comp, resp = rows[0]
    assert status == "completed"
    assert sub <= start <= comp
    assert json.loads(resp) == {"x": 7, "ok": True}


def test_bare_call_writes_pending_running_terminal_in_order_sync(default_recorder):
    """Sync mirror of the async lifecycle test."""
    seen: list[str] = []

    @recorder(kind="t.sync")
    def do(x):
        seen.extend(
            r[0]
            for r in default_recorder._connection()
            .execute("SELECT status FROM jobs WHERE kind=?", ("t.sync",))
            .fetchall()
        )
        return x * 2

    assert do(21) == 42
    assert seen == ["running"]

    row = default_recorder._connection().execute(
        "SELECT status, submitted_at, started_at, completed_at FROM jobs "
        "WHERE kind=?",
        ("t.sync",),
    ).fetchone()
    status, sub, start, comp = row
    assert status == "completed"
    assert sub <= start <= comp


# --- return value ----------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_call_returns_wrapped_functions_natural_value(default_recorder):
    """Named test: the decorator is transparent — no wrapping of the result."""

    sentinel = {"complex": ["value", 1, None]}

    @recorder(kind="t.return")
    async def returner():
        return sentinel

    out = await returner()
    assert out is sentinel


def test_bare_call_returns_wrapped_functions_natural_value_sync(default_recorder):
    """The decorator returns the function's exact value (no wrapping)."""

    @recorder(kind="t.return.sync")
    def returner():
        return [1, 2, "three"]

    assert returner() == [1, 2, "three"]


def test_unrecordable_response_marks_row_failed_and_reraises(default_recorder):
    """If the recorder can't serialize the response, mark the row failed
    rather than leaking it as `running`. The caller sees the TypeError so
    they know their function returned something unrecordable."""
    sentinel = object()

    @recorder(kind="t.unrecordable")
    def returner():
        return sentinel

    with pytest.raises(TypeError):
        returner()

    row = default_recorder._connection().execute(
        "SELECT status, error_json FROM jobs WHERE kind=?", ("t.unrecordable",)
    ).fetchone()
    assert row[0] == "failed"
    assert "recorder failed to serialize response" in row[1]


# --- failure path writes terminal ------------------------------------------


@pytest.mark.asyncio
async def test_bare_call_failure_writes_failed_terminal_and_reraises(default_recorder):
    @recorder(kind="t.boom")
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await boom()

    row = default_recorder._connection().execute(
        "SELECT status, error_json FROM jobs WHERE kind=?", ("t.boom",)
    ).fetchone()
    status, err = row
    assert status == "failed"
    assert json.loads(err) == {"type": "ValueError", "message": "nope"}


# --- request capture -------------------------------------------------------


@dataclass
class _Req:
    sku: str
    qty: int


@pytest.mark.asyncio
async def test_bare_call_serializes_request_via_adapter(default_recorder):
    @recorder(kind="t.req", request=_Req)
    async def take(req: _Req):
        return {"sku": req.sku}

    await take(_Req(sku="A", qty=3))
    raw = default_recorder._connection().execute(
        "SELECT request_json FROM jobs WHERE kind=?", ("t.req",)
    ).fetchone()[0]
    assert json.loads(raw) == {"sku": "A", "qty": 3}


# --- multi-arg request fallback --------------------------------------------


def test_multi_arg_request_captured_as_envelope(default_recorder):
    @recorder(kind="t.multi")
    def add(a, b, *, label="x"):
        return a + b

    add(1, 2, label="hi")
    raw = default_recorder._connection().execute(
        "SELECT request_json FROM jobs WHERE kind=?", ("t.multi",)
    ).fetchone()[0]
    assert json.loads(raw) == {"args": [1, 2], "kwargs": {"label": "hi"}}


# --- submit raises NotImplementedError -------------------------------------


def test_submit_raises_with_phase2_message(default_recorder):
    @recorder(kind="t.submit")
    def f(x):
        return x

    with pytest.raises(NotImplementedError, match="phase 2"):
        f.submit(1)
