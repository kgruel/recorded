"""`attach_error()` + `error=Model` wiring (Stream C 2.C.2).

The wrapper consults `JobContext.error_buffer` on its exception path
when the decorator was registered with `error=Model`. The wrapped
function's exception still propagates verbatim — `attach_error` only
re-shapes the recording.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from recorded import attach_error, recorder
from recorded._errors import AttachOutsideJobError


# --- shared fixtures -------------------------------------------------------


@dataclass
class BrokerError:
    code: str
    message: str
    correlation_id: str


# --- attach_error contract -------------------------------------------------


def test_attach_error_populates_error_slot_through_model_adapter(default_recorder):
    """`error=BrokerError` + `attach_error(BrokerError(...))` → `error_json`
    matches the model's `model_dump`/`asdict` shape."""

    @recorder(kind="t.err.basic", error=BrokerError)
    def fn(req):
        attach_error(BrokerError(code="E_RATE", message="rate limited", correlation_id="abc-123"))
        raise RuntimeError("rate-limited at upstream")

    with pytest.raises(RuntimeError, match="rate-limited at upstream"):
        fn({"x": 1})

    rows = default_recorder.last(1, kind="t.err.basic")
    assert len(rows) == 1
    job = rows[0]
    assert job.status == "failed"
    # `error=BrokerError` means the read API rehydrates as the dataclass.
    assert isinstance(job.error, BrokerError)
    assert job.error.code == "E_RATE"
    assert job.error.message == "rate limited"
    assert job.error.correlation_id == "abc-123"


def test_attach_error_full_replace_semantics_last_call_wins(default_recorder):
    """Two `attach_error()` calls keep only the second payload."""

    @recorder(kind="t.err.replace", error=BrokerError)
    def fn(req):
        attach_error(BrokerError(code="FIRST", message="first", correlation_id="c1"))
        attach_error(BrokerError(code="SECOND", message="second", correlation_id="c2"))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        fn({})

    rows = default_recorder.last(1, kind="t.err.replace")
    job = rows[0]
    assert job.error.code == "SECOND"
    assert job.error.correlation_id == "c2"


def test_attach_error_outside_job_raises():
    """Matches `attach()`'s `AttachOutsideJobError` precedent."""
    with pytest.raises(AttachOutsideJobError):
        attach_error({"anything": "at all"})


# --- error=Model fallback paths --------------------------------------------


def test_error_model_falls_back_to_type_message_when_attach_error_not_called(
    default_recorder,
):
    """`error=Model` is inert (default `{type, message}`) when the wrapped
    function never calls `attach_error()`."""

    @recorder(kind="t.err.fallback", error=BrokerError)
    def fn(req):
        raise RuntimeError("plain failure")

    with pytest.raises(RuntimeError):
        fn({})

    rows = default_recorder.last(1, kind="t.err.fallback")
    job = rows[0]
    # No `attach_error()` happened — the recorded error stays in the
    # default {type, message} shape. Read API rehydration through the
    # `error=BrokerError` adapter would fail on a {type, message} dict
    # because BrokerError requires (code, message, correlation_id), so
    # the rehydrator must surface the raw dict instead. The intent here
    # is "don't crash on the read path"; we tolerate either shape.
    raw = default_recorder._fetchone(
        "SELECT error_json FROM jobs WHERE id=?", (job.id,)
    )
    import json as _json
    err = _json.loads(raw[0])
    assert err == {"type": "RuntimeError", "message": "plain failure"}


def test_error_model_serialization_failure_falls_back_to_type_message(
    default_recorder,
):
    """A payload that can't be validated through the model still records
    the original `{type, message}`; the original exception is still raised."""

    @recorder(kind="t.err.bad_payload", error=BrokerError)
    def fn(req):
        # Missing `correlation_id` — dataclass construction will fail
        # inside `entry.error.serialize(...)`.
        attach_error({"code": "E_X", "message": "bad shape"})
        raise RuntimeError("the original error")

    with pytest.raises(RuntimeError, match="the original error"):
        fn({})

    rows = default_recorder.last(1, kind="t.err.bad_payload")
    raw = default_recorder._fetchone(
        "SELECT error_json FROM jobs WHERE id=?", (rows[0].id,)
    )
    import json as _json
    err = _json.loads(raw[0])
    # Fell back to the live exception's shape.
    assert err == {"type": "RuntimeError", "message": "the original error"}


# --- error=Model through the worker loop -----------------------------------


@pytest.mark.asyncio
async def test_attach_error_works_through_worker_loop(default_recorder):
    """`JobHandle` path: a worker-executed function calls `attach_error()`,
    raises; the row's `error_json` matches the model."""

    @recorder(kind="t.err.worker", error=BrokerError)
    async def fn(req):
        attach_error(BrokerError(code="W", message="worker-side", correlation_id="cw"))
        raise RuntimeError("worker boom")

    h = fn.submit({"x": 1})
    from recorded._errors import JoinedSiblingFailedError

    with pytest.raises(JoinedSiblingFailedError) as excinfo:
        await h.wait(timeout=5.0)
    # The sibling_error carries the recorded payload (as a dict — the
    # JoinedSiblingFailedError carries the raw dict, not the rehydrated
    # model, by design).
    assert excinfo.value.sibling_error == {
        "code": "W",
        "message": "worker-side",
        "correlation_id": "cw",
    }
    job = default_recorder.get(h.job_id)
    assert isinstance(job.error, BrokerError)
    assert job.error.code == "W"


# --- attach_error in passthrough mode (no error=Model) ---------------------


def test_attach_error_in_passthrough_mode_populates_error_slot(default_recorder):
    """`attach_error()` writes to `error_json` even when the decorator was
    NOT registered with `error=Model`. Passthrough mode accepts any
    JSON-shaped payload via `_to_native` — there is no reason to silently
    drop it just because no model was declared.
    """

    @recorder(kind="t.err.passthrough")
    def fn(req):
        attach_error({"code": "E_RATE", "retry_after": 30, "detail": "rate limited"})
        raise RuntimeError("rate-limited")

    with pytest.raises(RuntimeError):
        fn({"x": 1})

    rows = default_recorder.last(1, kind="t.err.passthrough")
    job = rows[0]
    assert job.status == "failed"
    # Passthrough rehydrates as a plain dict.
    assert job.error == {"code": "E_RATE", "retry_after": 30, "detail": "rate limited"}

