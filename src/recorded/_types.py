"""Public dataclasses returned from the read API."""

from __future__ import annotations

import json
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

    def to_prompt(self) -> str:
        """Markdown rendering of one Job for paste into an LLM.

        Audience is "an agent introspecting its own action history."
        Sections that are None/empty don't render — except Request,
        which is the audit anchor and always prints.
        """
        duration = (
            f"{self.duration_ms} ms" if self.duration_ms is not None else "—"
        )
        lines = [
            f"# {self.kind} — {self.status}",
            "",
            f"- id: {self.id}",
            f"- key: {self.key or '—'}",
            f"- submitted: {self.submitted_at}",
            f"- started:   {self.started_at or '—'}",
            f"- completed: {self.completed_at or '—'}",
            f"- duration:  {duration}",
            "",
            "## Request",
            "```json",
            _pretty(self.request) if self.request is not None else "—",
            "```",
        ]
        # Brief: "Slots that are None or empty don't print their section
        # header (except Request)." Response is gated on `is not None`
        # so legitimate falsy values (0, False, []) still render — audit
        # faithfulness. Data/error use truthiness because empty dicts
        # there are noise, not signal.
        if self.response is not None:
            lines += ["", "## Response", "```json", _pretty(self.response), "```"]
        if self.data:
            lines += ["", "## Data", "```json", _pretty(self.data), "```"]
        if self.error:
            lines += ["", "## Error", "```json", _pretty(self.error), "```"]
        return "\n".join(lines)


def _pretty(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=False, default=str)
