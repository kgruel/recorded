"""Read-API contract: `recorded.query`, `recorded.connection`,
`recorded.last(status=...)` for symmetry with `query()`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

import recorded
from recorded import Job, Recorder, _storage, recorder
from recorded._errors import ConfigurationError

# ----- connection -----


def test_recorded_connection_returns_sqlite_connection(default_recorder):
    """Named test: `recorded.connection()` returns the sqlite3 connection
    against the default Recorder. Raw SQL works against `jobs`."""
    conn = recorded.connection()
    assert isinstance(conn, sqlite3.Connection)
    # Schema is bootstrapped on first connection access.
    cur = conn.execute("SELECT count(*) FROM jobs")
    (n,) = cur.fetchone()
    assert n == 0

    # Same connection as the Recorder's _connection (public alias).
    assert conn is default_recorder._connection()


def test_recorder_connection_is_public_alias(recorder: Recorder):
    """`Recorder.connection()` is the public spelling of `_connection()`."""
    assert recorder.connection() is recorder._connection()


# ----- last(status=) symmetry -----


@pytest.mark.asyncio
async def test_last_with_status_filter(default_recorder):
    """Named test: `last(5, status='failed')` returns last 5 failed rows."""

    @recorder(kind="t.last.status")
    async def maybe_fail(should_fail: bool):
        if should_fail:
            raise RuntimeError("boom")
        return {"ok": True}

    # Mix of completed + failed.
    for should_fail in [True, False, True, False, True, True, False]:
        try:
            await maybe_fail(should_fail)
        except RuntimeError:
            pass

    failed = recorded.last(5, status="failed")
    assert all(j.status == "failed" for j in failed)
    assert len(failed) == 4  # only 4 failures total

    completed = recorded.last(5, status="completed")
    assert all(j.status == "completed" for j in completed)
    assert len(completed) == 3


# ----- query() filters -----


@pytest.mark.asyncio
async def test_recorded_query_filters_by_kind_status_since_until_where_data(
    default_recorder,
):
    """Named test: combined-filter cases that exercise each parameter."""

    from recorded import attach

    @recorder(kind="broker.place_order")
    async def place(req):
        attach("customer_id", req["customer_id"])
        return {"customer_id": req["customer_id"], "ok": True}

    @recorder(kind="broker.cancel_order")
    async def cancel(req):
        attach("customer_id", req["customer_id"])
        return {"customer_id": req["customer_id"], "cancelled": True}

    # Several rows across two kinds, two customers.
    await place({"customer_id": 7})  # 0
    await place({"customer_id": 9})  # 1
    await place({"customer_id": 7})  # 2
    await cancel({"customer_id": 7})  # 3
    await cancel({"customer_id": 9})  # 4

    # Filter by exact kind.
    only_place = list(recorded.query(kind="broker.place_order"))
    assert {j.kind for j in only_place} == {"broker.place_order"}
    assert len(only_place) == 3

    # Glob across both kinds.
    all_broker = list(recorded.query(kind="broker.*"))
    assert {j.kind for j in all_broker} == {"broker.place_order", "broker.cancel_order"}
    assert len(all_broker) == 5

    # `where_data` equality on top-level keys.
    cust7 = list(recorded.query(where_data={"customer_id": 7}))
    assert len(cust7) == 3  # 2 places + 1 cancel for cust 7

    # Multi-key where_data ANDs together.
    cust7_completed = list(recorded.query(status="completed", where_data={"customer_id": 7}))
    assert len(cust7_completed) == 3

    # since/until: clip to the future, expect empty.
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert list(recorded.query(since=future)) == []
    # until far in the past, expect empty.
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert list(recorded.query(until=past)) == []

    # since=str (already ISO) accepted.
    iso_now = _storage.now_iso()
    assert list(recorded.query(until=iso_now))  # at least some rows


@pytest.mark.asyncio
async def test_recorded_query_status_accepts_tuple_for_multi_status_query(
    default_recorder,
):
    """`query(status=("completed", "failed"))` issues a single SQL query
    using `status IN (...)` rather than forcing the caller to merge two
    queries."""

    @recorder(kind="t.query.multi_status")
    async def maybe_fail(should_fail: bool):
        if should_fail:
            raise RuntimeError("boom")
        return {"ok": True}

    for sf in [True, False, True, False]:
        try:
            await maybe_fail(sf)
        except RuntimeError:
            pass

    rows = list(
        recorded.query(
            kind="t.query.multi_status",
            status=("completed", "failed"),
        )
    )
    assert len(rows) == 4
    statuses = {r.status for r in rows}
    assert statuses == {"completed", "failed"}

    # Empty tuple is rejected — silently matching nothing would be a footgun.
    with pytest.raises(ConfigurationError, match="non-empty"):
        list(recorded.query(status=()))


@pytest.mark.asyncio
async def test_recorded_query_since_until_canonicalizes_non_canonical_strings(
    default_recorder,
):
    """`since=`/`until=` accept non-canonical ISO strings (no microseconds,
    no Z suffix). They're parsed and reformatted so lex-comparison against
    the canonical stored form is correct — no silent boundary off-by-one."""

    @recorder(kind="t.query.iso_canon")
    async def f(i: int):
        return {"i": i}

    for i in range(3):
        await f(i)

    # Stored timestamps are in canonical form (microseconds + Z). A non-
    # canonical user input must include all rows (since=well-in-the-past).
    far_past = "2000-01-01T00:00:00"  # no microseconds, no Z
    rows = list(recorded.query(kind="t.query.iso_canon", since=far_past))
    assert len(rows) == 3

    # And until=well-in-the-future must include all rows even with weird
    # but parseable suffix combinations.
    far_future = "2099-12-31T23:59:59"
    rows = list(recorded.query(kind="t.query.iso_canon", until=far_future))
    assert len(rows) == 3

    # A garbage string surfaces as ConfigurationError, not as a silent
    # mis-bound query.
    with pytest.raises(ConfigurationError, match="non-ISO8601"):
        list(recorded.query(since="not-a-date"))


@pytest.mark.asyncio
async def test_recorded_query_returns_iterator_lazily(default_recorder, monkeypatch):
    """Named test: `query()` returns an iterator (not a list); the SQL
    query isn't issued until first `next()`.

    We monkeypatch `Recorder._fetchall` and assert it's called on
    `next()`, not on the `query()` call itself.
    """

    # Insert a row so there's something to fetch.
    @recorder(kind="t.query.lazy")
    async def f():
        return {"v": 1}

    await f()

    calls: list[tuple] = []
    real_fetchall = Recorder._fetchall

    def spy(self, sql, params=()):
        calls.append((sql, params))
        return real_fetchall(self, sql, params)

    monkeypatch.setattr(Recorder, "_fetchall", spy)

    it = recorded.query(kind="t.query.lazy")
    # Iterator returned, but no fetchall yet.
    assert isinstance(it, Iterator)
    assert calls == []

    # First `next()` triggers the query.
    first = next(it)
    assert isinstance(first, Job)
    assert first.kind == "t.query.lazy"
    assert len(calls) == 1


def test_query_rejects_where_data_keys_with_dots_or_dollars(recorder: Recorder):
    """Reject `where_data` keys that look like JSONPath but aren't."""
    with pytest.raises(ConfigurationError, match=r"'\.' or '\$'"):
        list(recorder.query(where_data={"a.b": 1}))
    with pytest.raises(ConfigurationError, match=r"'\.' or '\$'"):
        list(recorder.query(where_data={"$.a": 1}))


def test_query_rejects_invalid_order(recorder: Recorder):
    with pytest.raises(ConfigurationError, match="must be 'asc' or 'desc'"):
        list(recorder.query(order="random"))


@pytest.mark.asyncio
async def test_recorded_query_filters_by_idempotency_key(default_recorder):
    """Named test: `query(key='kk')` returns only rows with that idempotency
    key (across kinds). Equality match on the `key` column."""

    @recorder(kind="t.query.bykey")
    async def fn(x):
        return {"x": x}

    # Seed: a couple of failed retries + a successful row, all under key 'kk',
    # plus one row under a different key, plus one with no key.
    @recorder(kind="t.query.bykey.flaky")
    async def flaky(x):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await flaky(1, key="kk")
    with pytest.raises(RuntimeError):
        await flaky(2, key="kk")
    await fn(3, key="kk")
    await fn(4, key="other")
    await fn(5)  # no key

    by_key = list(recorded.query(key="kk", limit=50))
    assert len(by_key) == 3
    assert {j.key for j in by_key} == {"kk"}

    # Filter combined with status — only the 2 failed retries.
    failed_under_kk = list(recorded.query(key="kk", status="failed"))
    assert len(failed_under_kk) == 2
    assert all(j.status == "failed" for j in failed_under_kk)


@pytest.mark.asyncio
async def test_query_order_asc_returns_oldest_first(default_recorder):
    @recorder(kind="t.query.order")
    async def f(x):
        return {"x": x}

    for i in range(3):
        await f(i)

    asc = list(recorded.query(kind="t.query.order", order="asc"))
    assert [j.request for j in asc] == [0, 1, 2]

    desc = list(recorded.query(kind="t.query.order", order="desc"))
    assert [j.request for j in desc] == [2, 1, 0]
