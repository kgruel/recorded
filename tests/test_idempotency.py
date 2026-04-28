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


# --- bug #3: typed-instance joiner gets the typed object, not the storage dict ---


@pytest.mark.asyncio
async def test_typed_instance_join_preserves_type_in_process(default_recorder, monkeypatch):
    """Same-process key-collision joiner receives the leader's live typed
    object, not the storage-rehydrated dict. Wrap-transparency holds for
    typed returns under `key=` without requiring `response=Model`.

    Cross-process joiners would still go through storage and need
    `response=Model` for typed returns — that is the documented advanced
    contract.
    """
    from dataclasses import dataclass

    @dataclass
    class OrderReply:
        order_id: str
        amount: int

    started = asyncio.Event()
    proceed = asyncio.Event()

    @recorder(kind="t.idem.typed_join")
    async def place_order(req):
        started.set()
        await proceed.wait()
        return OrderReply(order_id=f"order-{req}", amount=42)

    subscribed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_subscribe(_jid: str) -> None:
        loop.call_soon_threadsafe(subscribed.set)

    monkeypatch.setattr(default_recorder, "_for_testing_subscribe_callback", on_subscribe)

    leader_task = asyncio.create_task(place_order("A", key="kk"))
    await started.wait()

    joiner_task = asyncio.create_task(place_order("A", key="kk"))
    # Joiner has parked on `_subscribe`, so the live-result cache will
    # have a consumer when the leader resolves.
    await subscribed.wait()
    proceed.set()
    leader_result, joiner_result = await asyncio.gather(leader_task, joiner_task)

    # Leader gets its live result.
    assert isinstance(leader_result, OrderReply)
    assert leader_result.order_id == "order-A"

    # In-process joiner gets the typed instance via the live-result cache,
    # not a dict.
    assert isinstance(joiner_result, OrderReply)
    assert joiner_result.order_id == "order-A"
    assert joiner_result.amount == 42


# --- live-result cache hygiene (regression tests for the leak fix) ----------
#
# `_live_results` is a per-recorder in-process stash populated by
# `_run_and_record_async` immediately before the terminal write, so
# *same-process* idempotency joiners receive the leader's typed object
# rather than the storage-rehydrated dict.
#
# Under cross-process leadership the leader's stash lives in the *leader*
# process — the submitting process never populates its own
# `_live_results`. The .submit()-based variants of these tests pinned an
# in-process invariant that no longer applies; the bare-call variant
# below stays — that path still uses the in-process stash.


def test_keyed_bare_call_no_joiner_clears_cache_on_resolve(default_recorder):
    """Keyed bare-call with no sibling joiner: `_resolve` runs against an
    empty subscribers list and pops the stash, so the cache does not grow
    per-call."""

    @recorder(kind="t.live.bare_keyed_no_joiner")
    def fn(x):
        return {"x": x}

    out = fn(11, key="kk-bare-solo")
    assert out == {"x": 11}
    # Cache empty — `_resolve` cleared it on the no-subscriber path.
    assert len(default_recorder._live_results) == 0


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
