"""Idempotency at the call site, plus the auto-kind-+-key validation."""

from __future__ import annotations

import asyncio

import pytest

from recorded import attach, recorder
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
async def test_idempotency_collision_on_pending_waits_for_terminal(default_recorder, monkeypatch):
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

    # Fire `proceed.set()` once B has parked on `_subscribe` — that's
    # the precondition the test was approximating with `sleep(0.02)`.
    subscribed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_subscribe(_jid: str) -> None:
        loop.call_soon_threadsafe(subscribed.set)

    monkeypatch.setattr(default_recorder, "_for_testing_subscribe_callback", on_subscribe)

    a_task = asyncio.create_task(caller())
    # Wait until A has reached `running`. B will collide on INSERT and poll.
    await started.wait()
    b_task = asyncio.create_task(caller())
    # B has reached `_subscribe` on the active row.
    await subscribed.wait()
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


# --- failed + retry_failed=False: raise JoinedSiblingFailedError ----------


@pytest.mark.asyncio
async def test_idempotency_retry_failed_false_raises_joined_sibling_failed(
    default_recorder,
):
    """Named test: with `retry_failed=False`, a prior failure raises
    `JoinedSiblingFailedError` rather than returning a `Job`.

    Per the wrap-transparency principle: keyed call surfaces always
    either return the response OR raise — never return a `Job`. To
    inspect the prior failed row, use the read API
    (`recorded.query(kind=..., status='failed', ...)`)."""
    from recorded._errors import JoinedSiblingFailedError

    n = 0

    @recorder(kind="t.idem.no_retry")
    async def flaky():
        nonlocal n
        n += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await flaky(key="k")

    # Second call, retry_failed=False → raises with the sibling's error.
    with pytest.raises(JoinedSiblingFailedError) as excinfo:
        await flaky(key="k", retry_failed=False)
    assert excinfo.value.sibling_error == {"type": "ValueError", "message": "boom"}
    # No second execution happened.
    assert n == 1


# --- joiner response shape: symmetric across same-process / cross-process ---
#
# After dissolving the live-result cache, all joiners (same-process and
# cross-process) rehydrate the response from storage. Type identity for
# typed-instance returns under `key=` requires `response=Model`. The two
# tests below pin both halves of that contract for same-process joiners;
# cross-process joiners are exercised by the leader-driven suites.


@pytest.mark.asyncio
async def test_joiners_get_dict_without_response_model(default_recorder, monkeypatch):
    """Two same-process callers with the same `key=`, no `response=Model`
    declared: both get the dict shape from storage rehydration. Pins the
    post-cache contract — the leader's live typed object is no longer
    visible to the joiner."""
    from dataclasses import dataclass

    @dataclass
    class OrderReply:
        order_id: str
        amount: int

    started = asyncio.Event()
    proceed = asyncio.Event()

    @recorder(kind="t.idem.joiner_dict")
    async def place_order(req):
        started.set()
        await proceed.wait()
        return OrderReply(order_id=f"order-{req}", amount=42)

    subscribed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_subscribe(_jid: str) -> None:
        loop.call_soon_threadsafe(subscribed.set)

    monkeypatch.setattr(default_recorder, "_for_testing_subscribe_callback", on_subscribe)

    leader_task = asyncio.create_task(place_order("A", key="kk-dict"))
    await started.wait()

    joiner_task = asyncio.create_task(place_order("A", key="kk-dict"))
    await subscribed.wait()
    proceed.set()
    leader_result, joiner_result = await asyncio.gather(leader_task, joiner_task)

    # Leader returns the wrapped function's return value (wrap-transparent).
    assert isinstance(leader_result, OrderReply)

    # Joiner rehydrates from storage; without `response=Model` the
    # response column round-trips as a dict.
    assert not isinstance(joiner_result, OrderReply)
    assert joiner_result == {"order_id": "order-A", "amount": 42}


@pytest.mark.asyncio
async def test_joiners_preserve_type_with_response_model(default_recorder, monkeypatch):
    """Symmetric guarantee: with `response=Model` declared, the joiner's
    storage rehydration produces the typed instance — same shape the
    leader returned, no cache required."""
    from dataclasses import dataclass

    @dataclass
    class OrderReply:
        order_id: str
        amount: int

    started = asyncio.Event()
    proceed = asyncio.Event()

    @recorder(kind="t.idem.joiner_typed", response=OrderReply)
    async def place_order(req):
        started.set()
        await proceed.wait()
        return OrderReply(order_id=f"order-{req}", amount=42)

    subscribed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_subscribe(_jid: str) -> None:
        loop.call_soon_threadsafe(subscribed.set)

    monkeypatch.setattr(default_recorder, "_for_testing_subscribe_callback", on_subscribe)

    leader_task = asyncio.create_task(place_order("A", key="kk-typed"))
    await started.wait()

    joiner_task = asyncio.create_task(place_order("A", key="kk-typed"))
    await subscribed.wait()
    proceed.set()
    leader_result, joiner_result = await asyncio.gather(leader_task, joiner_task)

    assert isinstance(leader_result, OrderReply)
    assert isinstance(joiner_result, OrderReply)
    assert joiner_result.order_id == "order-A"
    assert joiner_result.amount == 42


# --- bug #4: data column round-trips for an idempotency joiner ------------


@pytest.mark.asyncio
async def test_typed_data_round_trips_for_idempotency_joiner(default_recorder, monkeypatch):
    """Round-trip guarantee for the data column under in-process
    idempotency joining.

    A typed `data=Model` row populated via `attach()` of a declared key
    is rehydrated cleanly via the public read API — both the leader's
    write and the joiner's read see the same shape. Pre-fix this would
    have either raised at the read path (undeclared key) or silently
    dropped extras; the typed-data attach contract turns both into
    "declared key, clean rehydration."
    """
    from dataclasses import dataclass

    @dataclass
    class OrderView:
        order_id: str
        note: str | None = None

    started = asyncio.Event()
    proceed = asyncio.Event()

    @recorder(kind="t.idem.data_round_trip", data=OrderView)
    async def place_order(req):
        started.set()
        await proceed.wait()
        attach("note", "fast lane")
        return OrderView(order_id=f"order-{req}")

    subscribed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_subscribe(_jid: str) -> None:
        loop.call_soon_threadsafe(subscribed.set)

    monkeypatch.setattr(default_recorder, "_for_testing_subscribe_callback", on_subscribe)

    leader_task = asyncio.create_task(place_order("A", key="kk-data"))
    await started.wait()

    joiner_task = asyncio.create_task(place_order("A", key="kk-data"))
    await subscribed.wait()
    proceed.set()
    await asyncio.gather(leader_task, joiner_task)

    # Single row, both writers landed on the same one.
    rows = default_recorder.last(2, kind="t.idem.data_round_trip")
    assert len(rows) == 1
    job = rows[0]

    # Read-path rehydrates the data column to an OrderView instance with
    # the attached `note` populated — pre-fix this branch would have
    # raised TypeError on an undeclared key. Now it round-trips.
    assert isinstance(job.data, OrderView)
    assert job.data.order_id == "order-A"
    assert job.data.note == "fast lane"


# --- IdempotencyRaceError raise sites (post-INSERT lookup returns None) ----
#
# Three call paths each raise `IdempotencyRaceError` when the post-INSERT
# active-row lookup finds nothing — the narrow window where the prior
# holder cleaned up between our INSERT-violation and our lookup.
# Practically rare; tests force the path via monkeypatch and assert the
# error's kind / key attributes are populated.


def test_submit_raises_idempotency_race_when_post_insert_lookup_returns_none(
    leader_recorder, monkeypatch
):
    """`.submit(key=K)` site (`_decorator._submit`).

    Uses `leader_recorder` so `.submit()` passes the leader-presence gate
    (step 5). The leader subprocess won't claim the seeded row because
    its kind isn't registered in the leader process — but the test
    raises `IdempotencyRaceError` at `.submit()` time, before any wait.
    """
    from recorded import _storage as _storage_mod
    from recorded._errors import IdempotencyRaceError

    rec = leader_recorder

    @recorder(kind="t.race.submit")
    def fn(x):
        return x

    # Pre-seed a `running` row so our INSERT collides on the partial
    # unique index.
    blocker_id = _storage_mod.new_id()
    now = _storage_mod.now_iso()
    rec._insert_running(blocker_id, "t.race.submit", "kk-race-submit", now, now, '"x"')

    # Force the active-row lookup to return None on both the pre-INSERT
    # check (which then proceeds to the INSERT) and the post-INSERT
    # recovery (which then raises).
    monkeypatch.setattr(rec, "_lookup_active_by_kind_key", lambda kind, key: None)

    with pytest.raises(IdempotencyRaceError) as excinfo:
        fn.submit(1, key="kk-race-submit")
    assert excinfo.value.kind == "t.race.submit"
    assert excinfo.value.key == "kk-race-submit"


def test_sync_bare_call_raises_idempotency_race_when_post_insert_lookup_returns_none(
    default_recorder, monkeypatch
):
    """Sync bare-call site (`_decorator._wait_for_join`)."""
    from recorded import _storage as _storage_mod
    from recorded._errors import IdempotencyRaceError

    rec = default_recorder

    @recorder(kind="t.race.bare_sync")
    def fn(x):
        return x

    blocker_id = _storage_mod.new_id()
    now = _storage_mod.now_iso()
    rec._insert_running(blocker_id, "t.race.bare_sync", "kk-race-sync", now, now, '"x"')
    monkeypatch.setattr(rec, "_lookup_active_by_kind_key", lambda kind, key: None)

    with pytest.raises(IdempotencyRaceError) as excinfo:
        fn(1, key="kk-race-sync")
    assert excinfo.value.kind == "t.race.bare_sync"
    assert excinfo.value.key == "kk-race-sync"


@pytest.mark.asyncio
async def test_async_bare_call_raises_idempotency_race_when_post_insert_lookup_returns_none(
    default_recorder, monkeypatch
):
    """Async bare-call site (`_decorator._async_wait_for_join`)."""
    from recorded import _storage as _storage_mod
    from recorded._errors import IdempotencyRaceError

    rec = default_recorder

    @recorder(kind="t.race.bare_async")
    async def fn(x):
        return x

    blocker_id = _storage_mod.new_id()
    now = _storage_mod.now_iso()
    rec._insert_running(blocker_id, "t.race.bare_async", "kk-race-async", now, now, '"x"')
    monkeypatch.setattr(rec, "_lookup_active_by_kind_key", lambda kind, key: None)

    with pytest.raises(IdempotencyRaceError) as excinfo:
        await fn(1, key="kk-race-async")
    assert excinfo.value.kind == "t.race.bare_async"
    assert excinfo.value.key == "kk-race-async"
