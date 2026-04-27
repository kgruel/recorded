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


@dataclass
class JobContext:
    """Per-call context carried in a contextvar.

    `recorder` is the Recorder driving the call (used by `attach(flush=True)`
    in phase 1.3 to write through). `buffer` accumulates `attach()` pairs
    that get flushed at completion.
    """

    job_id: str
    kind: str
    recorder: Any  # forward ref to Recorder; avoid circular import
    buffer: dict[str, Any] = field(default_factory=dict)


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
