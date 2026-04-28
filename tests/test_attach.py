"""attach() contextvar plumbing + end-to-end buffer/flush behaviors."""

from __future__ import annotations

import asyncio
import json

import pytest

from recorded import attach, recorder
from recorded._context import JobContext, current_job


def test_attach_outside_job_is_a_silent_noop():
    """Basic-feature-set wrap-transparency: removing `@recorder` and
    forgetting to remove the `attach()` calls inside the function should
    not explode the caller. Side-effect-only APIs go silent when there
    is nothing to act on.
    """
    # Should not raise.
    attach("k", "v")
    attach("k", "v", flush=True)


def test_attach_buffers_into_current_context():
    ctx = JobContext(job_id="j", kind="k", recorder=None, entry=None)
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
        ctx = JobContext(job_id=name, kind="k", recorder=None, entry=None)
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
    """attach() merges over a `data=` projection of the response.

    Both attached keys (`"a"`) are declared on the data model, satisfying
    the typed-data attach contract.
    """
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


# --- typed `data=Model` + attach() contract -------------------------------
#
# `attach(k, v)` under `@recorder(data=Model)` must validate `k` against
# the model's declared fields. Unknown keys raise `AttachKeyError` at the
# call site, before any DB write — preserving wrap-transparency: a row
# written via the public write path must round-trip through the public
# read path.


def test_attach_typed_data_declared_key_round_trips(default_recorder):
    """Regression seed: typed `data=Model` + attach(declared_key) used
    to write a valid `data_json` document but crash the read-path
    rehydration on the unknown key. Now the key must be declared, so
    rehydration succeeds end-to-end via `recorded.last()`.
    """
    from dataclasses import dataclass

    @dataclass
    class OrderView:
        order_id: str
        note: str | None = None

    @recorder(kind="t.contract.declared", data=OrderView)
    def fn():
        attach("note", "fast lane")
        return OrderView(order_id="o1")

    fn()

    rows = default_recorder.last(1, kind="t.contract.declared")
    assert len(rows) == 1
    job = rows[0]
    assert isinstance(job.data, OrderView)
    assert job.data.order_id == "o1"
    assert job.data.note == "fast lane"


def test_attach_typed_data_undeclared_key_raises_at_call_site(default_recorder):
    """Pydantic data=Model + undeclared attach key → AttachKeyError at
    the attach() call site, with structured attributes pointing at the
    offending key, the model, and the declared field set.
    """
    from recorded import AttachKeyError

    pydantic = pytest.importorskip("pydantic")
    BaseModel = pydantic.BaseModel

    class OrderView(BaseModel):
        order_id: str
        total: float

    @recorder(kind="t.contract.undeclared.pyd", data=OrderView)
    def fn():
        attach("rogue", 1)  # <-- not in OrderView
        return OrderView(order_id="o1", total=1.0)

    with pytest.raises(AttachKeyError) as excinfo:
        fn()

    err = excinfo.value
    assert err.kind == "t.contract.undeclared.pyd"
    assert err.key == "rogue"
    assert err.model is OrderView
    assert err.declared == frozenset({"order_id", "total"})
    # Message names the offending key and lists declared fields.
    msg = str(err)
    assert "'rogue'" in msg
    assert "OrderView" in msg
    assert "order_id" in msg
    assert "total" in msg


def test_attach_bare_recorder_accepts_any_key_unchanged(default_recorder):
    """Bare `@recorder` (no data=) keeps free-form passthrough — escape
    hatch for experimentation. Pins the basic-tier behavior."""

    @recorder(kind="t.contract.bare")
    def fn():
        attach("anything_goes", "ok")
        attach("nested", {"deep": [1, 2, 3]})
        return None

    fn()

    rows = default_recorder.last(1, kind="t.contract.bare")
    assert rows[0].data == {"anything_goes": "ok", "nested": {"deep": [1, 2, 3]}}


def test_attach_dataclass_data_undeclared_key_raises(default_recorder):
    """Dataclass `data=Model` + undeclared attach key → AttachKeyError.

    Mirrors the pydantic test independently so the dataclass path is
    covered without relying on pydantic being installed.
    """
    from dataclasses import dataclass

    from recorded import AttachKeyError

    @dataclass
    class OrderViewDc:
        order_id: str
        total: float

    @recorder(kind="t.contract.undeclared.dc", data=OrderViewDc)
    def fn():
        attach("rogue", 1)
        return OrderViewDc(order_id="o1", total=1.0)

    with pytest.raises(AttachKeyError) as excinfo:
        fn()

    assert excinfo.value.model is OrderViewDc
    assert excinfo.value.declared == frozenset({"order_id", "total"})


def test_attach_pydantic_alias_field_uses_canonical_name(default_recorder):
    """Aliased pydantic field: canonical name attaches, alias raises.

    Pins the canonical-name decision: stored data_json is keyed by
    canonical names (model_dump default), so attaching by alias would
    create a key the rehydration path cannot reach.
    """
    from recorded import AttachKeyError

    pydantic = pytest.importorskip("pydantic")
    BaseModel = pydantic.BaseModel
    Field = pydantic.Field

    class WithAlias(BaseModel):
        # Configure populate_by_name so the read-back can validate
        # data_json by its canonical name regardless of alias config.
        model_config = {"populate_by_name": True}
        canonical_name: str = Field(alias="aliased_name", default="x")
        other: str = "y"

    # Canonical name attaches cleanly.
    @recorder(kind="t.contract.alias.canonical", data=WithAlias)
    def good():
        attach("canonical_name", "set")
        return None

    good()
    rows = default_recorder.last(1, kind="t.contract.alias.canonical")
    assert isinstance(rows[0].data, WithAlias)
    assert rows[0].data.canonical_name == "set"

    # Alias name raises — declared field set is canonical names only.
    @recorder(kind="t.contract.alias.bad", data=WithAlias)
    def bad():
        attach("aliased_name", "set")
        return None

    with pytest.raises(AttachKeyError) as excinfo:
        bad()
    assert excinfo.value.key == "aliased_name"


def test_attach_flush_true_validates_before_write(default_recorder):
    """attach(undeclared, v, flush=True) raises before touching the DB.

    Catches the regression of moving validation into _build_data_json:
    flush=True bypasses the buffer and would silently land an
    unreachable key. Validation must happen at the attach() call site
    so both buffered and flushed paths are covered uniformly.
    """
    from dataclasses import dataclass

    from recorded import AttachKeyError

    @dataclass
    class OrderView:
        order_id: str

    @recorder(kind="t.contract.flush", data=OrderView)
    def fn():
        attach("rogue", 1, flush=True)
        return OrderView(order_id="o1")

    with pytest.raises(AttachKeyError):
        fn()

    # data_json never received the rogue write — the row is `failed` (the
    # AttachKeyError propagated through _run_and_record), and its
    # data_json column is NULL because no flush happened.
    raw = (
        default_recorder._connection()
        .execute("SELECT data_json, status FROM jobs WHERE kind=?", ("t.contract.flush",))
        .fetchone()
    )
    assert raw[0] is None
    assert raw[1] == "failed"


def test_attach_outside_recorded_context_skips_validation():
    """Even when typed `data=Model` exists in the registry, attach()
    outside any active context is still a silent no-op.

    Wrap-transparency: removing `@recorder` from a function should not
    require also removing the `attach()` calls in its body. The
    validation only fires when there is an active context to validate
    against.
    """
    from dataclasses import dataclass

    @dataclass
    class OrderView:
        order_id: str

    # Register a typed entry in the global registry.
    @recorder(kind="t.contract.outside", data=OrderView)
    def fn():
        return OrderView(order_id="o1")

    # Do NOT call fn() — call attach() at top level instead. Should not raise.
    attach("undeclared", "value")
    attach("undeclared", "value", flush=True)
