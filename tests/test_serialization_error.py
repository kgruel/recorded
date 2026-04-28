"""`SerializationError.slot` for the user-reachable slot-rejection paths.

Two paths surface `SerializationError` to caller code, each with a
distinct slot tag:

1. Request validation at decorator entry — `slot="request"`. The bare
   call site sees this; no row is written, the wrapped function does
   not run.
2. Read-API deserialization — `slot="data"`/`slot="response"`. A row
   written under one model gets rehydrated under a model whose schema
   no longer matches; `recorded.last/get/query` raises with the slot
   tag identifying which JSON column rejected.

Post-success serialization in `_lifecycle._write_completion` is NOT a
catchable case — wrap-transparency requires the bare-call surface
never raise an exception class the wrapped function couldn't, so those
errors are converted to `recorded`-logger warnings + a `failed` row.
This test file deliberately does not cover that path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from recorded import recorder
from recorded._errors import SerializationError


@dataclass
class _Reply:
    ok: bool
    note: str


@dataclass
class _DataView:
    customer_id: int


# ----- request slot: validation at decorator entry --------------------


def test_request_serialization_error_carries_slot_request(default_recorder):
    """Caller passes a value the request adapter can't validate. The
    bare-call site raises `SerializationError(slot="request")` before
    the wrapped function runs.
    """

    @recorder(kind="t.serror.request", request=_Reply)
    def fn(req):
        return {"received": True}

    with pytest.raises(SerializationError) as exc_info:
        fn({"ok": True})  # missing required `note`
    assert exc_info.value.slot == "request"
    assert exc_info.value.model is _Reply


# ----- data / response slots: read-API deserialization ----------------


def test_data_deserialization_error_carries_slot_data(default_recorder):
    """A row written when no `data=Model` was registered is then read
    under a registered `data=Model` whose schema doesn't fit. The
    rehydration raises with `slot="data"`.

    Direct-write fixture: insert a raw row whose `data_json` won't
    validate against `_DataView`, then register a kind with that data
    model and read it back.
    """
    import recorded
    from recorded import _storage

    kind = "t.serror.data"

    # Insert a raw row first — no decorator registration yet, so no
    # data adapter rejects on write.
    conn = recorded.connection()
    conn.execute(
        f"INSERT INTO jobs ({', '.join(_storage.COLUMNS)}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "row1",
            kind,
            None,
            "completed",
            _storage.now_iso(),
            _storage.now_iso(),
            _storage.now_iso(),
            None,
            None,
            json.dumps({"unrelated_key": "x"}),  # won't fit _DataView
            None,
        ),
    )

    # Now register the kind with a typed data adapter — read back and
    # the rehydration trips on the row's stored data shape.
    @recorder(kind=kind, data=_DataView)
    def fn():
        return {"customer_id": 1}

    with pytest.raises(SerializationError) as exc_info:
        recorded.get("row1")
    assert exc_info.value.slot == "data"


def test_response_deserialization_error_carries_slot_response(default_recorder):
    """Same shape as the data test, but the response slot."""
    import recorded
    from recorded import _storage

    kind = "t.serror.response"

    conn = recorded.connection()
    conn.execute(
        f"INSERT INTO jobs ({', '.join(_storage.COLUMNS)}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "row2",
            kind,
            None,
            "completed",
            _storage.now_iso(),
            _storage.now_iso(),
            _storage.now_iso(),
            None,
            json.dumps({"ok": True}),  # missing 'note'
            None,
            None,
        ),
    )

    @recorder(kind=kind, response=_Reply)
    def fn():
        return _Reply(ok=True, note="ok")

    with pytest.raises(SerializationError) as exc_info:
        recorded.get("row2")
    assert exc_info.value.slot == "response"
