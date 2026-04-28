"""`.submit()` requires a fresh leader heartbeat (Option A gate).

Pinned here:

- Without a leader, `.submit()` raises `ConfigurationError` *before*
  inserting any row. The error message names the fix
  (`python -m recorded run`).
- Bare-call paths (sync wrapper / async wrapper / `attach()`) do NOT
  query the leader heartbeat — Tier 1 stays free of leader-detection
  cost. PLAN.md §6.1.
- The check runs after `_validate_call_args`, so existing validation
  errors (multi-arg `.submit()`, `key=` against auto-kind) continue to
  surface their original messages.
"""

from __future__ import annotations

import pytest

from recorded import recorder
from recorded._errors import ConfigurationError


def test_submit_without_leader_raises_configuration_error(default_recorder):
    """`default_recorder` has no leader subprocess — `.submit()` must
    refuse loudly rather than insert a row that nothing will execute."""

    @recorder(kind="t.gate.no_leader")
    def fn(x):
        return {"x": x}

    with pytest.raises(ConfigurationError, match=r"requires a running leader process"):
        fn.submit(1)


def test_submit_without_leader_does_not_insert_pending_row(default_recorder):
    """Loud-failure property: the gate fires before `_insert_pending`,
    so a refused `.submit()` leaves no detritus in the audit log."""

    @recorder(kind="t.gate.no_pending")
    def fn(x):
        return {"x": x}

    with pytest.raises(ConfigurationError):
        fn.submit("nope")

    rows = default_recorder._fetchall(
        "SELECT id FROM jobs WHERE kind=?",
        ("t.gate.no_pending",),
    )
    assert rows == []


def test_submit_validation_error_takes_precedence_over_leader_gate(default_recorder):
    """Multi-arg `.submit()` raises its `_validate_call_args` error first,
    not the leader-gate error. The check order is intentional —
    validation errors describe a coding bug, the gate describes an
    operational state. Code bugs surface even without a leader running.
    """

    @recorder(kind="t.gate.multi_args")
    def fn(a, b):
        return {"a": a, "b": b}

    with pytest.raises(ConfigurationError, match=r"requires single-positional-arg"):
        fn.submit(1, 2)


def test_bare_call_does_not_query_leader_heartbeat(default_recorder, monkeypatch):
    """Tier 1 (`@recorder` bare call) must not pay leader-detection cost.

    Spies `Recorder._is_leader_running` and asserts zero invocations on
    a sync bare call, an async bare call, and the cross-mode shims.
    PLAN.md §6.1.
    """
    import asyncio

    calls = []

    def spy_is_leader_running(self):
        calls.append(self.path)
        return False

    monkeypatch.setattr(
        type(default_recorder),
        "_is_leader_running",
        spy_is_leader_running,
        raising=True,
    )

    @recorder(kind="t.gate.bare_sync")
    def sync_fn(x):
        return {"x": x}

    @recorder(kind="t.gate.bare_async")
    async def async_fn(x):
        return {"x": x}

    assert sync_fn(1) == {"x": 1}
    assert asyncio.run(async_fn(2)) == {"x": 2}

    # Cross-mode shims also bare-call under the hood.
    assert sync_fn.sync(3) == {"x": 3}
    assert asyncio.run(async_fn.async_run(4)) == {"x": 4}

    assert calls == [], f"bare calls must not query leader heartbeat, got {len(calls)} call(s)"


def test_submit_with_leader_succeeds(leader_recorder):
    """Sanity: with a leader running, `.submit()` proceeds past the gate.

    Regression-pin against accidentally inverting the gate condition.
    """
    import sys

    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    try:
        import _leader_kinds
    finally:
        sys.path.pop(0)

    h = _leader_kinds.echo.submit("ok")
    job = h.wait_sync(timeout=5.0)
    assert job.status == "completed"
