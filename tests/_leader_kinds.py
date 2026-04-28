"""Test-only @recorder decorations for leader-subprocess tests.

Loaded into the leader process via `python -m recorded run --import
_leader_kinds` (the subprocess gets PYTHONPATH=tests/ so this module
resolves). The decorations register kinds in `_registry` so the leader
can dispatch `pending` rows we seed against those kinds.

The kinds are intentionally namespaced under `t.leader.*` to keep them
distinguishable from production kinds; they're only meaningful inside
the test suite.

`_ensure_registered()` is the bridge to the autouse `_clean_registry`
fixture (`tests/conftest.py`): module-level decorations only run on
first import, so after `_registry._reset()` wipes the registry, the
wrapper objects still exist but the registry doesn't. Calling
`_ensure_registered()` from the `leader_recorder` fixture re-registers
the wrappers' entries — no module reload, no side effects.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from recorded import _registry, attach, attach_error, recorder


@dataclass
class LeaderError:
    """Test error model used by `attach_error_demo` to verify the
    `error=Model` adapter via the leader path."""

    code: str
    message: str
    correlation_id: str


@recorder(kind="t.leader.echo")
def echo(req):
    """Sync function that echoes its input — simplest possible job."""
    return {"echoed": req}


@recorder(kind="t.leader.echo_async")
async def echo_async(req):
    """Async variant — exercises the worker's coroutine dispatch."""
    await asyncio.sleep(0.001)
    return {"echoed_async": req}


@recorder(kind="t.leader.attach_demo")
def attach_demo(req):
    """Exercises the `attach()` ContextVar plumbing inside the leader process."""
    attach("k", req)
    return {"ok": True}


@recorder(kind="t.leader.fails")
def fails(req):
    """Always raises — tests the failure-recording path."""
    raise RuntimeError(f"intentional failure: {req!r}")


@recorder(kind="t.leader.slow_echo")
def slow_echo(req):
    """Sleep `req['sleep']` seconds then echo `req['value']`.

    Used by idempotency-collision tests that need the row to remain
    `running` long enough for the second `.submit()` call to find it.
    Cross-process, so cross-process synchronization (events) doesn't
    work — duration-based timing does.
    """
    time.sleep(float(req.get("sleep", 0.0)))
    return {"value": req.get("value")}


@recorder(kind="t.leader.slow_echo_async")
async def slow_echo_async(req):
    """Async variant of `slow_echo`."""
    await asyncio.sleep(float(req.get("sleep", 0.0)))
    return {"value": req.get("value")}


@recorder(kind="t.leader.attach_error_demo", error=LeaderError)
async def attach_error_demo(req):
    """Calls `attach_error()` then raises — exercises `error=Model` via the
    leader's `_run_and_record_async`."""
    attach_error(
        LeaderError(
            code=req.get("code", "W"),
            message=req.get("message", "leader-side"),
            correlation_id=req.get("correlation_id", "cw"),
        )
    )
    raise RuntimeError(req.get("raise_message", "leader boom"))


# Functions registered by the @recorder decorations above. Ordered so
# `_ensure_registered` is exhaustive even if a test introspects this list.
ALL_KINDS = (
    echo,
    echo_async,
    attach_demo,
    fails,
    slow_echo,
    slow_echo_async,
    attach_error_demo,
)


def _ensure_registered() -> None:
    """Re-register every wrapper's RegistryEntry.

    No-op if already registered (`_registry.register` last-write-wins).
    Necessary after the autouse `_clean_registry` fixture wipes the
    process registry between tests.
    """
    for fn in ALL_KINDS:
        _registry.register(fn._entry)
