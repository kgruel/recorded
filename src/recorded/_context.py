"""ContextVars for the currently-executing recorded function.

`current_job` carries a `JobContext` whenever execution is inside a
recorded wrapper. `attach()` consults this contextvar to find the buffer
to write into.

contextvars propagate naturally through `await` and across `asyncio.gather`
(each task gets its own context copy), so concurrent recorded calls in
different tasks see isolated buffers.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from ._errors import AttachOutsideJobError


_UNSET: Any = object()


@dataclass
class JobContext:
    """Per-call context carried in a contextvar.

    `recorder` is the Recorder driving the call (used by `attach(flush=True)`
    in phase 1.3 to write through). `buffer` accumulates `attach()` pairs
    that get flushed at completion. `error_buffer` is a single-slot
    full-replace payload populated by `attach_error()` when the wrapper
    has `error=Model` registered; the wrapper consults it on the failure
    path. Sentinel `_UNSET` distinguishes "never set" from "set to None".
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
    at completion. `flush=True` writes through immediately via json_patch
    (implemented in phase 1.3).
    """
    ctx = current_job.get()
    if ctx is None:
        raise AttachOutsideJobError(
            "attach() called outside a recorded function. "
            "It must be called from within the body of a function decorated "
            "with @recorder."
        )
    ctx.buffer[key] = value
    if flush:
        # Phase 1.3 will implement immediate write-through via json_patch.
        # For phase 1.1 the buffer behavior is the only one exercised.
        ctx.recorder._flush_attach(ctx.job_id, key, value)


def attach_error(payload: Any) -> None:
    """Stash a structured error payload for the running job's `error` slot.

    Mirrors `attach()` but writes to the single-slot `error_buffer` with
    full-replace semantics (last call wins). The wrapper consults this
    on the exception path and serializes it through the registered
    `error=Model` adapter; if no `attach_error()` was called the wrapper
    falls back to the default `{type, message}` shape from the original
    exception.

    The wrapped function's exception still propagates verbatim —
    `attach_error()` only re-shapes the *recording*, not the raise.
    Wrap-transparency: removing `@recorder` and the `attach_error()`
    call doesn't change the exception the caller sees.
    """
    ctx = current_job.get()
    if ctx is None:
        raise AttachOutsideJobError(
            "attach_error() called outside a recorded function. "
            "It must be called from within the body of a function decorated "
            "with @recorder."
        )
    ctx.error_buffer = payload
