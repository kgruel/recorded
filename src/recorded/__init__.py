"""recorded — typed function-call recorder backed by SQLite.

Public surface:
    - `Recorder` class (connection, lifecycle, read API, worker, reaper)
    - `recorder` decorator
    - `attach(key, value, *, flush=False)` for mid-execution annotations
    - `attach_error(payload)` for structured error payloads on the failure path
    - `JobHandle` returned from `.submit()`
    - `Job` dataclass
    - Module-level read API: `get`, `last`, `query`, `connection`
    - `configure(...)` for the module-level default Recorder
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from ._context import attach, attach_error
from ._decorator import recorder
from ._errors import (
    ConfigurationError,
    IdempotencyError,
    IdempotencyRaceError,
    JoinedSiblingFailedError,
    JoinTimeoutError,
    RecordedError,
    RecorderClosedError,
    SerializationError,
    SyncInLoopError,
    UsageError,
)
from ._handle import JobHandle
from ._recorder import Recorder, configure, get_default
from ._types import Job


def get(job_id: str) -> Job | None:
    """Read a job by id from the default Recorder."""
    return get_default().get(job_id)


def last(
    n: int = 10,
    *,
    kind: str | None = None,
    status: str | None = None,
) -> list[Job]:
    """Most recent `n` jobs, optionally filtered by kind glob and status."""
    return get_default().last(n, kind=kind, status=status)


def query(
    kind: str | None = None,
    status: str | None = None,
    key: str | None = None,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    where_data: dict[str, Any] | None = None,
    limit: int = 100,
    order: str = "desc",
) -> Iterator[Job]:
    """Filtered iterator over jobs from the default Recorder."""
    return get_default().query(
        kind=kind,
        status=status,
        key=key,
        since=since,
        until=until,
        where_data=where_data,
        limit=limit,
        order=order,
    )


def connection() -> sqlite3.Connection:
    """Escape hatch: raw SQLite connection on the default Recorder."""
    return get_default().connection()


__all__ = [
    # Core API
    "Recorder",
    "Job",
    "JobHandle",
    "attach",
    "attach_error",
    "configure",
    "connection",
    "get",
    "last",
    "query",
    "recorder",
    # Exception hierarchy — re-exported so callers can `except recorded.X:`
    # without reaching into `recorded._errors`.
    "RecordedError",
    "UsageError",
    "IdempotencyError",
    "ConfigurationError",
    "RecorderClosedError",
    "SerializationError",
    "SyncInLoopError",
    "IdempotencyRaceError",
    "JoinedSiblingFailedError",
    "JoinTimeoutError",
]
