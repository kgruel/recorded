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
