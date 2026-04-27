"""Exception hierarchy."""

from __future__ import annotations


class RecordedError(Exception):
    """Root for library-raised errors."""


class ConfigurationError(RecordedError):
    """Misuse at decoration / configuration time (raised eagerly)."""


class AttachOutsideJobError(RecordedError):
    """`attach()` called when no recorded function is on the call stack."""


class SyncInLoopError(RecordedError):
    """`.sync()` invoked from within a running event loop."""
