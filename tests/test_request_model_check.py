"""Multi-arg `request=Model` call-time check (Stream C 2.C.2).

The capture envelope for multi-arg / kwarg calls is `{args, kwargs}` —
a shape that can't be validated through a typed request model. Mirrors
the `key=` + auto-kind precedent: misuse caught at call time, not
silently mis-recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from recorded import recorder
from recorded._errors import ConfigurationError


@dataclass
class PlaceOrder:
    customer_id: int
    quantity: int


def test_request_model_with_multi_positional_args_raises_configuration_error(
    default_recorder,
):
    @recorder(kind="t.req.multi_args", request=PlaceOrder)
    def fn(a, b):
        return {"a": a, "b": b}

    with pytest.raises(ConfigurationError, match="request=Model requires single-positional-arg"):
        fn(1, 2)


def test_request_model_with_kwargs_raises_configuration_error(default_recorder):
    @recorder(kind="t.req.with_kwargs", request=PlaceOrder)
    def fn(req, *, extra=None):
        return {"req": req, "extra": extra}

    with pytest.raises(ConfigurationError, match="request=Model requires single-positional-arg"):
        fn(PlaceOrder(customer_id=1, quantity=1), extra="x")


def test_request_model_single_positional_arg_still_works(default_recorder):
    """The check only fires for multi-arg shapes; the supported
    single-positional shape still works."""

    @recorder(kind="t.req.single", request=PlaceOrder)
    def fn(req):
        return {"customer_id": req.customer_id}

    out = fn(PlaceOrder(customer_id=42, quantity=2))
    assert out == {"customer_id": 42}


def test_submit_with_multi_positional_args_raises_configuration_error(
    default_recorder,
):
    """`.submit()` round-trips the captured request through JSON to the
    worker. Multi-arg / kwarg envelopes can't be unpacked unambiguously
    on the worker side, so the surface refuses them at submit time."""

    @recorder(kind="t.submit.multi_args")
    def fn(a, b):
        return {"a": a, "b": b}

    with pytest.raises(ConfigurationError, match=r"\.submit\(\) requires single-positional-arg"):
        fn.submit(1, 2)


def test_submit_with_kwargs_raises_configuration_error(default_recorder):
    @recorder(kind="t.submit.with_kwargs")
    def fn(req, *, extra=None):
        return {"req": req, "extra": extra}

    with pytest.raises(ConfigurationError, match=r"\.submit\(\) requires single-positional-arg"):
        fn.submit("hello", extra="x")


def test_submit_with_single_positional_arg_still_works(leader_recorder):
    """Single-positional-arg `.submit()` is the supported shape.

    Uses `_leader_kinds.echo` so the leader subprocess can execute it —
    closure-defined kinds aren't visible cross-process.
    """
    import sys

    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    try:
        import _leader_kinds
    finally:
        sys.path.pop(0)

    handle = _leader_kinds.echo.submit(99)
    job = handle.wait_sync(timeout=5.0)
    assert job.response == {"echoed": 99}
