"""Public dataclasses returned from the read API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _parse_iso(s: str) -> datetime:
    # ISO8601 UTC with `Z` suffix; `fromisoformat` handles `Z` since Python 3.11.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


@dataclass
class Job:
    """Read-side representation of a recorded call.

    `request`, `response`, `data`, `error` are rehydrated to typed values
    when a model was registered for the kind; otherwise raw dicts.
    """

    id: str
    kind: str
    key: str | None
    status: str
    submitted_at: str
    started_at: str | None
    completed_at: str | None
    request: Any
    response: Any
    data: Any | None
    error: Any | None

    @property
    def duration_ms(self) -> int | None:
        """Wall time from started_at to completed_at, in ms.

        `None` until both timestamps are set (i.e. terminal state).
        """
        if not self.started_at or not self.completed_at:
            return None
        delta = _parse_iso(self.completed_at) - _parse_iso(self.started_at)
        return int(delta.total_seconds() * 1000)
