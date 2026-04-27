"""Read API slice (get, last) — rehydrates rows through the registry.

We bypass the (not-yet-implemented) decorator by registering kinds
directly and inserting rows via raw SQL. This validates the rehydration
boundary in isolation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

import recorded
from recorded import _registry, _storage
from recorded._adapter import Adapter


@dataclass
class _OrderView:
    customer_id: int
    total_cents: int


@dataclass
class _OrderReq:
    sku: str
    qty: int


def _insert_completed(
    conn,
    *,
    job_id: str,
    kind: str,
    key: str | None = None,
    request: dict | None = None,
    response: dict | None = None,
    data: dict | None = None,
):
    now = _storage.now_iso()
    conn.execute(
        "INSERT INTO jobs "
        "(id, kind, key, status, submitted_at, started_at, completed_at, "
        " request_json, response_json, data_json) "
        "VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            kind,
            key,
            now,
            now,
            now,
            json.dumps(request) if request is not None else None,
            json.dumps(response) if response is not None else None,
            json.dumps(data) if data is not None else None,
        ),
    )


def test_recorded_get_returns_rehydrated_typed_values(default_recorder):
    """Named test: registered kinds rehydrate JSON into model instances."""
    _registry.register(
        _registry.RegistryEntry(
            kind="orders.place",
            request=Adapter(_OrderReq),
            data=Adapter(_OrderView),
        )
    )

    conn = default_recorder._connection()
    _insert_completed(
        conn,
        job_id="job-1",
        kind="orders.place",
        request={"sku": "ABC", "qty": 3},
        response={"id": "ext-1", "ok": True},  # no model -> raw dict
        data={"customer_id": 42, "total_cents": 9900},
    )

    job = recorded.get("job-1")

    assert job is not None
    assert job.id == "job-1"
    assert job.kind == "orders.place"
    assert job.status == "completed"
    assert job.request == _OrderReq(sku="ABC", qty=3)
    assert job.response == {"id": "ext-1", "ok": True}  # passthrough
    assert job.data == _OrderView(customer_id=42, total_cents=9900)
    assert job.error is None
    assert job.duration_ms is not None  # terminal => set


def test_recorded_get_unknown_id_returns_none(default_recorder):
    assert recorded.get("nope") is None


def test_recorded_get_unregistered_kind_returns_raw_dicts(default_recorder):
    conn = default_recorder._connection()
    _insert_completed(
        conn,
        job_id="job-2",
        kind="other.thing",
        request={"hello": "world"},
        response=[1, 2, 3],
    )
    job = recorded.get("job-2")
    assert job is not None
    assert job.request == {"hello": "world"}
    assert job.response == [1, 2, 3]


def test_recorded_last_returns_most_recent_first_with_kind_glob(default_recorder):
    conn = default_recorder._connection()
    for n, kind in enumerate(["a.x", "broker.place", "broker.cancel", "other.q"]):
        _insert_completed(conn, job_id=f"id-{n}", kind=kind)

    all_jobs = recorded.last(10)
    assert [j.id for j in all_jobs] == ["id-3", "id-2", "id-1", "id-0"]

    broker_only = recorded.last(10, kind="broker.*")
    assert {j.id for j in broker_only} == {"id-1", "id-2"}
    # ordering still desc within the filter
    assert [j.id for j in broker_only] == ["id-2", "id-1"]


def test_recorded_last_respects_limit(default_recorder):
    conn = default_recorder._connection()
    for n in range(5):
        _insert_completed(conn, job_id=f"id-{n}", kind="k.x")
    assert len(recorded.last(2)) == 2


def test_recorder_shutdown_is_idempotent(db_path):
    from recorded._errors import RecorderClosedError

    r = recorded.Recorder(path=db_path)
    r._connection()
    r.shutdown()
    r.shutdown()  # second call must not raise
    with pytest.raises(RecorderClosedError):
        r._connection()
