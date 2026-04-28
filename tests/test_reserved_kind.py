"""Reserved-kind enforcement: `_recorded.*` is library-internal.

Two contracts pinned here:

1. `@recorder(kind="_recorded.foo")` raises `ConfigurationError` at
   decoration time. Users can't accidentally collide with the library's
   internal rows (leader heartbeats).
2. The read API (`Recorder.query`/`last`, module-level `recorded.query`/
   `last`) excludes `_recorded.*` rows by default. Explicit
   `kind="_recorded.*"` opts in.
"""

from __future__ import annotations

import pytest

from recorded import recorder
from recorded._errors import ConfigurationError


def test_recorder_rejects_reserved_kind_prefix():
    """Decoration-time check: `_recorded.*` is reserved."""
    with pytest.raises(ConfigurationError, match=r"reserved prefix"):

        @recorder(kind="_recorded.leader")
        def f(x):
            return x


def test_recorder_rejects_reserved_kind_prefix_async():
    with pytest.raises(ConfigurationError, match=r"reserved prefix"):

        @recorder(kind="_recorded.something")
        async def f(x):
            return x


def test_recorder_accepts_kind_starting_with_recorded_but_not_reserved():
    """The reserved prefix is exact: `_recorded.` (with the leading
    underscore and trailing dot). `recorded.foo` (no leading underscore)
    is fine; `_recorder.foo` (different word) is fine.
    """

    @recorder(kind="recorded.foo")
    def fn1(x):
        return x

    @recorder(kind="_recorder.foo")
    def fn2(x):
        return x

    # Neither raised — both are user-namespaced kinds.
    assert fn1.kind == "recorded.foo"
    assert fn2.kind == "_recorder.foo"
