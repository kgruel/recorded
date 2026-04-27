"""Phase-1 integration suite — exercises the recorder against real HTTP I/O.

Every test in here drives a real httpx client against a real WSGI server
(`pytest_httpserver`) running in-process on a random port. We do not mock
the transport — sockets, headers, JSON-on-the-wire are all live. The only
thing we mock is the user's external (the broker), via the test server.

The suite is deliberately bigger on the concurrent and idempotency paths
since those are the financial-safety bets of the library and the unit
suite stops at state-machine logic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import dataclass

import httpx
import pytest
from werkzeug.wrappers import Response

from recorded import Recorder, attach, get, last, recorder
from recorded import _recorder as _recorder_mod
from recorded._errors import SerializationError, SyncInLoopError


# --- helpers --------------------------------------------------------------


def _broker_response(order_id: str = "ord-1", **extra) -> dict:
    """Broker-API-shaped payload — nested dict, optional fields, mixed types."""
    payload = {
        "order_id": order_id,
        "status": "accepted",
        "filled_qty": 0,
        "is_maker": True,
        "fees": {"taker_bps": 10, "maker_bps": 5},
        "tags": ["live", "us-equity"],
    }
    payload.update(extra)
    return payload


# --- 1. 50 concurrent async calls -----------------------------------------


@pytest.mark.asyncio
async def test_fifty_concurrent_async_calls_all_recorded(default_recorder, httpserver):
    """All 50 callers complete, all rows are `completed`, timestamps coherent,
    rehydrated request/response match what crossed the wire."""

    def handler(request):
        body = json.loads(request.get_data(as_text=True))
        return Response(
            json.dumps(_broker_response(order_id=f"ord-{body['n']}", n=body["n"])),
            content_type="application/json",
        )

    httpserver.expect_request("/orders").respond_with_handler(handler)

    @recorder(kind="ext.broker.place")
    async def place(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(httpserver.url_for("/orders"), json=req)
            r.raise_for_status()
            return r.json()

    results = await asyncio.gather(*(place({"n": i}) for i in range(50)))

    # Each caller sees the response keyed to its own request.
    assert sorted(r["n"] for r in results) == list(range(50))

    rows = (
        default_recorder._connection()
        .execute(
            "SELECT status, submitted_at, started_at, completed_at, "
            "       request_json, response_json "
            "FROM jobs WHERE kind=? ORDER BY submitted_at",
            ("ext.broker.place",),
        )
        .fetchall()
    )
    assert len(rows) == 50
    for status, sub, start, comp, req_json, resp_json in rows:
        assert status == "completed"
        assert sub <= start <= comp
        req = json.loads(req_json)
        resp = json.loads(resp_json)
        assert resp["n"] == req["n"]
        assert resp["order_id"] == f"ord-{req['n']}"


# --- 2. 50 concurrent idempotent calls → one HTTP request -----------------


@pytest.mark.asyncio
async def test_fifty_concurrent_idempotent_make_one_real_request(
    default_recorder, httpserver
):
    """The financial-safety test. 50 callers, same `key`, gathered. Exactly
    one real HTTP request hits the broker; all 50 callers see the same
    response value."""

    # The handler holds open briefly so 50 INSERT-races can happen against
    # the partial unique index. Without the hold, the winner could finish
    # before the others reach the join path — losing the contention signal.
    release = threading.Event()
    seen_count = 0
    seen_lock = threading.Lock()

    def handler(request):
        nonlocal seen_count
        with seen_lock:
            seen_count += 1
        release.wait(timeout=2.0)
        return Response(
            json.dumps(_broker_response(order_id="ord-singleton")),
            content_type="application/json",
        )

    httpserver.expect_request("/orders").respond_with_handler(handler)

    @recorder(kind="ext.broker.idem")
    async def place(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(httpserver.url_for("/orders"), json=req)
            r.raise_for_status()
            return r.json()

    async def caller():
        return await place({"qty": 1}, key="order-42")

    tasks = [asyncio.create_task(caller()) for _ in range(50)]
    # Let the first INSERT happen and the others collide on the unique index
    # before we release the server.
    await asyncio.sleep(0.05)
    release.set()

    results = await asyncio.gather(*tasks)

    # The financial-safety assertion.
    assert len(httpserver.log) == 1, (
        f"expected exactly one real request; got {len(httpserver.log)}"
    )
    assert seen_count == 1
    # All 50 callers got the same response value.
    for r in results:
        assert r["order_id"] == "ord-singleton"

    # Exactly one active row exists for (kind, key) — partial unique index
    # invariant.
    count = (
        default_recorder._connection()
        .execute(
            "SELECT count(*) FROM jobs WHERE kind=? AND key=?",
            ("ext.broker.idem", "order-42"),
        )
        .fetchone()[0]
    )
    assert count == 1


# --- 3. Sequential idempotency collision ----------------------------------


@pytest.mark.asyncio
async def test_sequential_idempotency_returns_rehydrated_without_reexecution(
    default_recorder, httpserver
):
    """Caller A finishes, then B–E join sequentially: each gets the
    rehydrated response, no additional HTTP call."""

    httpserver.expect_request("/orders").respond_with_json(
        _broker_response(order_id="ord-seq")
    )

    @recorder(kind="ext.broker.seq")
    async def place(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(httpserver.url_for("/orders"), json=req)
            r.raise_for_status()
            return r.json()

    a = await place({"qty": 1}, key="ord-seq")
    b = await place({"qty": 2}, key="ord-seq")  # different request, same key
    c = await place({"qty": 3}, key="ord-seq")
    d = await place({"qty": 4}, key="ord-seq")
    e = await place({"qty": 5}, key="ord-seq")

    # All callers receive A's response.
    for r in (a, b, c, d, e):
        assert r["order_id"] == "ord-seq"
    # Server saw exactly one POST.
    assert len(httpserver.log) == 1
    # And the recorded request reflects A's input, not B–E's.
    recorded_req = json.loads(
        default_recorder._connection()
        .execute(
            "SELECT request_json FROM jobs WHERE kind=? AND key=?",
            ("ext.broker.seq", "ord-seq"),
        )
        .fetchone()[0]
    )
    assert recorded_req == {"qty": 1}


# --- 4. attach() trail under real I/O -------------------------------------


@pytest.mark.asyncio
async def test_attach_trail_under_real_io(default_recorder, httpserver):
    """A wrapped function does a real HTTP call, then attaches a
    correlation_id from the response. data_json contains the attach."""

    httpserver.expect_request("/orders").respond_with_json(
        _broker_response(order_id="ord-7", broker_correlation_id="corr-XYZ")
    )

    @recorder(kind="ext.broker.attach")
    async def place(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(httpserver.url_for("/orders"), json=req)
            r.raise_for_status()
            body = r.json()
        attach("correlation_id", body["broker_correlation_id"])
        attach("status_code", r.status_code)
        return body

    await place({"qty": 1})

    raw = (
        default_recorder._connection()
        .execute(
            "SELECT data_json FROM jobs WHERE kind=?", ("ext.broker.attach",)
        )
        .fetchone()[0]
    )
    data = json.loads(raw)
    assert data["correlation_id"] == "corr-XYZ"
    assert data["status_code"] == 200


# --- 5. Adapter tiers all record realistic payloads -----------------------


@dataclass
class _OrderReqDC:
    sku: str
    qty: int


@dataclass
class _OrderViewDC:
    order_id: str
    filled_qty: int


@pytest.mark.asyncio
async def test_dataclass_adapter_records_broker_payload(default_recorder, httpserver):
    httpserver.expect_request("/dc").respond_with_json(_broker_response(order_id="dc-1"))

    @recorder(kind="ext.tier.dc", request=_OrderReqDC, data=_OrderViewDC)
    async def place(req: _OrderReqDC) -> dict:
        async with httpx.AsyncClient() as c:
            return (await c.post(httpserver.url_for("/dc"), json={"sku": req.sku})).json()

    await place(_OrderReqDC(sku="ABC", qty=3))
    job = last(1, kind="ext.tier.dc")[0]
    assert job.request == _OrderReqDC(sku="ABC", qty=3)
    # Full response preserved (audit invariant).
    assert job.response["fees"] == {"taker_bps": 10, "maker_bps": 5}
    # data projection picks out the declared fields.
    assert job.data == _OrderViewDC(order_id="dc-1", filled_qty=0)


@pytest.mark.asyncio
async def test_pydantic_adapter_records_broker_payload(default_recorder, httpserver):
    pydantic = pytest.importorskip("pydantic")

    class OrderReq(pydantic.BaseModel):
        sku: str
        qty: int

    class OrderView(pydantic.BaseModel):
        order_id: str
        filled_qty: int

    httpserver.expect_request("/pyd").respond_with_json(_broker_response(order_id="pyd-1"))

    @recorder(kind="ext.tier.pyd", request=OrderReq, data=OrderView)
    async def place(req: OrderReq) -> dict:
        async with httpx.AsyncClient() as c:
            return (
                await c.post(httpserver.url_for("/pyd"), json={"sku": req.sku})
            ).json()

    await place(OrderReq(sku="XYZ", qty=2))
    job = last(1, kind="ext.tier.pyd")[0]
    assert isinstance(job.request, OrderReq)
    assert job.request.sku == "XYZ"
    assert isinstance(job.data, OrderView)
    assert job.data.order_id == "pyd-1"


@pytest.mark.asyncio
async def test_plain_dict_passthrough_records_broker_payload(default_recorder, httpserver):
    httpserver.expect_request("/dict").respond_with_json(
        _broker_response(order_id="dict-1")
    )

    @recorder(kind="ext.tier.dict")
    async def place(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            return (await c.post(httpserver.url_for("/dict"), json=req)).json()

    await place({"sku": "PLAIN", "qty": 1, "tif": None, "is_test": False})
    job = last(1, kind="ext.tier.dict")[0]
    # Raw passthrough — no model.
    assert job.request == {"sku": "PLAIN", "qty": 1, "tif": None, "is_test": False}
    assert job.response["order_id"] == "dict-1"


# --- 6. 5xx → row failed + useful error_json ------------------------------


@pytest.mark.asyncio
async def test_server_500_marks_row_failed_and_propagates_exception(
    default_recorder, httpserver
):
    """The wrapped function's HTTPStatusError propagates verbatim (we don't
    wrap user-domain exceptions). The row is `failed` and the recorded
    error captures the type and message."""

    httpserver.expect_request("/orders").respond_with_data(
        "broker exploded", status=500
    )

    @recorder(kind="ext.broker.5xx")
    async def place(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(httpserver.url_for("/orders"), json=req)
            r.raise_for_status()
            return r.json()

    with pytest.raises(httpx.HTTPStatusError):
        await place({"qty": 1})

    row = (
        default_recorder._connection()
        .execute(
            "SELECT status, error_json FROM jobs WHERE kind=?",
            ("ext.broker.5xx",),
        )
        .fetchone()
    )
    status, err_json = row
    assert status == "failed"
    err = json.loads(err_json)
    assert err["type"] == "HTTPStatusError"
    assert "500" in err["message"]


# --- 7. Slow response records correct duration_ms -------------------------


@pytest.mark.asyncio
async def test_slow_response_records_realistic_duration(default_recorder, httpserver):
    def handler(request):
        time.sleep(0.2)
        return Response(json.dumps({"ok": True}), content_type="application/json")

    httpserver.expect_request("/slow").respond_with_handler(handler)

    @recorder(kind="ext.broker.slow")
    async def place() -> dict:
        async with httpx.AsyncClient() as c:
            return (await c.get(httpserver.url_for("/slow"))).json()

    await place()
    job = last(1, kind="ext.broker.slow")[0]
    assert job.duration_ms is not None
    assert job.duration_ms >= 200
    assert job.duration_ms < 2000


# --- 8. Query real recorded data ------------------------------------------


@pytest.mark.asyncio
async def test_query_real_recorded_data(default_recorder, httpserver):
    """Exercise the read API against rows produced by real recorded calls.

    `recorded.list()` and `recorded.connection()` are specced in DESIGN.md
    but not implemented in phase 1 (only `get()` and `last()` ship). We
    drive the equivalents through `last()` + `Recorder._connection()` and
    document the API gap in PROGRESS_INTEGRATION.md.
    """
    httpserver.expect_request("/customers").respond_with_handler(
        lambda req: Response(
            json.dumps(
                {
                    "customer_id": json.loads(req.get_data(as_text=True))["customer_id"],
                    "ok": True,
                }
            ),
            content_type="application/json",
        )
    )

    @recorder(kind="ext.api.lookup")
    async def lookup(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(httpserver.url_for("/customers"), json=req)
            return r.json()

    @recorder(kind="ext.api.audit")
    async def audit(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(httpserver.url_for("/customers"), json=req)
            return r.json()

    # Seed a mix of rows. attach() puts customer_id in data_json so we can
    # exercise the where_data shape via raw SQL (since recorded.list is
    # not implemented in phase 1).
    @recorder(kind="ext.api.with_data")
    async def with_data(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(httpserver.url_for("/customers"), json=req)
            body = r.json()
        attach("customer_id", body["customer_id"])
        return body

    for cid in (1, 2, 3, 7, 7, 9):
        await with_data({"customer_id": cid})
    await lookup({"customer_id": 100})
    await audit({"customer_id": 200})

    # last(N) — most recent first, no filter.
    recent = last(3)
    assert len(recent) == 3
    # last(N, kind=...) — exact kind filter.
    audits = last(10, kind="ext.api.audit")
    assert len(audits) == 1 and audits[0].kind == "ext.api.audit"
    # Glob filter via last(): "ext.api.*" should pull all 8 rows.
    glob = last(50, kind="ext.api.*")
    assert len(glob) == 8

    # Equivalent of recorded.list(where_data={"customer_id": 7}) — raw SQL
    # via the Recorder's connection (recorded.connection() does not exist
    # in phase 1; flagged as gap in PROGRESS_INTEGRATION.md).
    rows = (
        default_recorder._connection()
        .execute(
            "SELECT id FROM jobs "
            "WHERE json_extract(data_json, '$.customer_id') = ? "
            "ORDER BY submitted_at",
            (7,),
        )
        .fetchall()
    )
    assert len(rows) == 2  # both customer_id=7 rows


# --- 9. Unrecordable response under real load -----------------------------


@pytest.mark.asyncio
async def test_unrecordable_response_under_load_marks_failed_and_raises_serialization(
    default_recorder, httpserver
):
    """50 concurrent calls; each wrapped fn returns a non-JSON-serializable
    object after a real HTTP roundtrip. Every caller sees SerializationError
    (not the underlying TypeError); every row ends `failed`."""

    httpserver.expect_request("/x").respond_with_json({"ok": True})

    class _NotSerializable:
        pass

    @recorder(kind="ext.broker.bad_response")
    async def place(req: dict):
        async with httpx.AsyncClient() as c:
            await c.post(httpserver.url_for("/x"), json=req)
        return _NotSerializable()  # unrecordable

    async def caller(i):
        with pytest.raises(SerializationError):
            await place({"i": i})

    await asyncio.gather(*(caller(i) for i in range(50)))

    rows = (
        default_recorder._connection()
        .execute(
            "SELECT status FROM jobs WHERE kind=?",
            ("ext.broker.bad_response",),
        )
        .fetchall()
    )
    assert len(rows) == 50
    assert all(r[0] == "failed" for r in rows)


# --- 10. Sync decorator wrapping sync httpx.Client ------------------------


def test_sync_decorator_with_sync_httpx_client(default_recorder, httpserver):
    httpserver.expect_request("/sync").respond_with_json(
        _broker_response(order_id="sync-1")
    )

    @recorder(kind="ext.broker.sync")
    def place(req: dict) -> dict:
        with httpx.Client() as c:
            r = c.post(httpserver.url_for("/sync"), json=req)
            r.raise_for_status()
            return r.json()

    out = place({"sku": "S", "qty": 1})
    assert out["order_id"] == "sync-1"

    job = last(1, kind="ext.broker.sync")[0]
    assert job.status == "completed"
    assert job.request == {"sku": "S", "qty": 1}
    assert job.response["order_id"] == "sync-1"


# --- 11. .async_run with real sync I/O from async caller ------------------


@pytest.mark.asyncio
async def test_async_run_does_not_block_loop_during_real_sync_io(
    default_recorder, httpserver
):
    """`.async_run` runs the sync function on the threadpool via
    `asyncio.to_thread`, so the event loop stays responsive. We prove this
    by ticking a counter task while the sync HTTP call is in flight; if
    the loop were blocked, the counter would not advance."""

    def handler(request):
        time.sleep(0.2)
        return Response(json.dumps({"ok": True}), content_type="application/json")

    httpserver.expect_request("/blocking").respond_with_handler(handler)

    @recorder(kind="ext.broker.async_run")
    def fetch() -> dict:
        with httpx.Client() as c:
            return c.get(httpserver.url_for("/blocking")).json()

    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.02)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await fetch.async_run()
    finally:
        stop.set()
        await ticker_task

    assert result == {"ok": True}
    # During the ~200ms sync fetch the ticker should have fired several
    # times. Assert >= 3 to leave headroom for scheduler jitter.
    assert ticks >= 3, f"event loop appears blocked; ticker fired {ticks} times"

    job = last(1, kind="ext.broker.async_run")[0]
    assert job.status == "completed"


# --- 12. .sync from inside an event loop raises SyncInLoopError -----------


@pytest.mark.asyncio
async def test_sync_from_inside_event_loop_raises(default_recorder, httpserver):
    httpserver.expect_request("/x").respond_with_json({"ok": True})

    @recorder(kind="ext.broker.sync_in_loop")
    async def place(req: dict) -> dict:
        async with httpx.AsyncClient() as c:
            return (await c.post(httpserver.url_for("/x"), json=req)).json()

    with pytest.raises(SyncInLoopError, match=r"running event loop"):
        place.sync({"qty": 1})


# --- 13. .submit() returns a JobHandle (Stream A.2 worker is wired) -------


def test_submit_returns_job_handle_resolving_to_terminal_job(default_recorder):
    """Phase 2 Stream A wired `.submit()` through the worker. Each
    submit returns a JobHandle whose `wait_sync()` resolves to a Job."""
    from recorded import JobHandle

    @recorder(kind="ext.broker.submit_real")
    def fn(x):
        return {"echo": x}

    handle = fn.submit(42)
    assert isinstance(handle, JobHandle)
    job = handle.wait_sync(timeout=5.0)
    assert job.status == "completed"
    assert job.response == {"echo": 42}
