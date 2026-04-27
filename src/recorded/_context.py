"""ContextVars for the currently-executing recorded function.

`current_job` carries a `JobContext` whenever execution is inside a
recorded wrapper. `attach()` and `attach_error()` consult this contextvar
to find the buffer to write into; both no-op when no context is set, so
removing `@recorder` (the basic-feature-set wrap-transparency contract)
leaves call-site `attach()`/`attach_error()` calls as silent no-ops
rather than raising.

contextvars propagate naturally through `await` and across `asyncio.gather`
(each task gets its own context copy), so concurrent recorded calls in
different tasks see isolated buffers.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


_UNSET: Any = object()


@dataclass
class JobContext:
    """Per-call context carried in a contextvar.

    `recorder` is the Recorder driving the call (used by `attach(flush=True)`
    for immediate write-through). `buffer` accumulates `attach()` pairs
    that get flushed at completion. `error_buffer` is a single-slot
    full-replace payload populated by `attach_error()`; the recording
    layer consults it on the failure path. Sentinel `_UNSET` distinguishes
    "never set" from "set to None".
    """

    job_id: str
    kind: str
    recorder: Any  # forward ref to Recorder; avoid circular import
    buffer: dict[str, Any] = field(default_factory=dict)
    error_buffer: Any = field(default=_UNSET)


current_job: ContextVar[JobContext | None] = ContextVar("recorded_current_job", default=None)


def attach(key: str, value: Any, *, flush: bool = False) -> None:
    """Stash a key/value pair into the running job's data buffer.

    By default the pair lands in an in-memory buffer that is flushed once
    at completion. `flush=True` writes through immediately via json_patch.

    Outside a recorded context, `attach()` is a silent no-op. Removing
    `@recorder` from a function should not require also removing all the
    `attach()` calls in its body.
    """
    ctx = current_job.get()
    if ctx is None:
        return
    ctx.buffer[key] = value
    if flush:
        ctx.recorder._flush_attach(ctx.job_id, key, value)


def attach_error(payload: Any) -> None:
    """Stash a structured error payload for the running job's `error` slot.

    Mirrors `attach()` but writes to the single-slot `error_buffer` with
    full-replace semantics (last call wins). The recording layer routes
    the payload through the slot adapter on the exception path —
    `error=Model` validates it through the model; passthrough renders
    via `_to_native`. If no `attach_error()` was called, the recording
    falls back to `{type, message}` from the original exception.

    The wrapped function's exception still propagates verbatim —
    `attach_error()` only re-shapes the *recording*, not the raise.
    Outside a recorded context, `attach_error()` is a silent no-op for
    the same reason `attach()` is: removing `@recorder` shouldn't require
    rewriting the function body.
    """
    ctx = current_job.get()
    if ctx is None:
        return
    ctx.error_buffer = payload
