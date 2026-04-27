"""recorded — typed function-call recorder backed by SQLite.

Phase 1.1 surface:
    - `Recorder` class (connection management, read methods)
    - `attach(key, value)` for mid-execution annotations
    - `get(job_id)` / `last(n, kind=...)` module-level read API
    - `Job` dataclass

The decorator (`recorder`) and write lifecycle land in phase 1.2.
"""

from __future__ import annotations

from ._context import attach
from ._decorator import recorder
from ._recorder import Recorder, get_default
from ._types import Job


def get(job_id: str) -> Job | None:
    """Read a job by id from the default Recorder."""
    return get_default().get(job_id)


def last(n: int = 10, *, kind: str | None = None) -> list[Job]:
    """Most recent `n` jobs, optionally filtered by kind glob."""
    return get_default().last(n, kind=kind)


__all__ = ["Recorder", "Job", "attach", "get", "last", "recorder"]
