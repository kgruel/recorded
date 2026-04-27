"""Type adapter and Job behaviors.

The type adapter is the one place where we use property-based testing
(per the brief): random dataclass / Pydantic / dict shapes round-trip
through the adapter losslessly.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, strategies as st

from recorded._adapter import Adapter
from recorded._types import Job

# --- Pydantic gating ---------------------------------------------------------

try:
    import pydantic  # type: ignore  # noqa: F401

    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False


# --- example-based --------------------------------------------------------


@dataclass
class _OrderView:
    customer_id: int
    total_cents: int
    note: str | None = None


def test_dataclass_roundtrip_via_data_slot():
    """Named test: dataclass model survives serialize -> deserialize."""
    a = Adapter(_OrderView)
    src = _OrderView(customer_id=42, total_cents=9900, note="hi")
    raw = a.serialize(src)
    assert raw == {"customer_id": 42, "total_cents": 9900, "note": "hi"}
    rehydrated = a.deserialize(raw)
    assert rehydrated == src


def test_dataclass_accepts_dict_input_and_validates_by_construction():
    a = Adapter(_OrderView)
    raw = a.serialize({"customer_id": 7, "total_cents": 100, "note": None})
    assert raw == {"customer_id": 7, "total_cents": 100, "note": None}
    # missing required field raises at serialize time (dict path).
    with pytest.raises(TypeError):
        a.serialize({"customer_id": 1})  # missing total_cents


def test_passthrough_adapter_stores_dicts_verbatim():
    a = Adapter()  # model=None
    src = {"anything": [1, 2, 3], "nested": {"k": "v"}}
    assert a.serialize(src) == src
    assert a.deserialize(src) == src
    assert a.serialize(None) is None
    assert a.deserialize(None) is None


def test_unsupported_model_type_raises():
    class NotAModel:
        pass

    with pytest.raises(TypeError):
        Adapter(NotAModel)


# --- pydantic ---------------------------------------------------------------


@pytest.mark.skipif(not HAS_PYDANTIC, reason="pydantic not installed")
def test_pydantic_roundtrip_when_installed():
    """Named test: Pydantic v2 model survives serialize -> deserialize."""
    from pydantic import BaseModel

    class Order(BaseModel):
        customer_id: int
        total_cents: int

    a = Adapter(Order)
    src = Order(customer_id=42, total_cents=9900)
    raw = a.serialize(src)
    assert raw == {"customer_id": 42, "total_cents": 9900}
    rehydrated = a.deserialize(raw)
    assert rehydrated == src
    # dict input is validated through the model
    rehydrated2 = a.serialize({"customer_id": 1, "total_cents": 2})
    assert rehydrated2 == {"customer_id": 1, "total_cents": 2}


# --- property-based: random shapes round-trip -------------------------------


_PRIMITIVE = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
    st.booleans(),
    st.none(),
)
_JSON_VAL = st.recursive(
    _PRIMITIVE,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4),
    ),
    max_leaves=8,
)


@given(st.dictionaries(st.text(min_size=1, max_size=8), _JSON_VAL, max_size=5))
def test_passthrough_adapter_property_roundtrip(d):
    a = Adapter()
    assert a.deserialize(a.serialize(d)) == d


@dataclass
class _Bag:
    a: int
    b: str
    c: list[int]


@given(
    st.builds(
        _Bag,
        a=st.integers(),
        b=st.text(max_size=20),
        c=st.lists(st.integers(), max_size=5),
    )
)
def test_dataclass_adapter_property_roundtrip(bag):
    a = Adapter(_Bag)
    assert a.deserialize(a.serialize(bag)) == bag


@pytest.mark.skipif(not HAS_PYDANTIC, reason="pydantic not installed")
@given(
    st.integers(),
    st.text(max_size=20),
    st.lists(st.integers(), max_size=5),
)
def test_pydantic_adapter_property_roundtrip(i, s, lst):
    from pydantic import BaseModel

    class Bag(BaseModel):
        a: int
        b: str
        c: list[int]

    a = Adapter(Bag)
    src = Bag(a=i, b=s, c=lst)
    assert a.deserialize(a.serialize(src)) == src


# --- Job.duration_ms --------------------------------------------------------


def test_job_duration_ms_none_until_terminal():
    j = Job(
        id="x",
        kind="k",
        key=None,
        status="running",
        submitted_at="2026-04-26T00:00:00.000000Z",
        started_at="2026-04-26T00:00:00.500000Z",
        completed_at=None,
        request=None,
        response=None,
        data=None,
        error=None,
    )
    assert j.duration_ms is None


def test_job_duration_ms_computed_from_start_and_complete():
    j = Job(
        id="x",
        kind="k",
        key=None,
        status="completed",
        submitted_at="2026-04-26T00:00:00.000000Z",
        started_at="2026-04-26T00:00:00.000000Z",
        completed_at="2026-04-26T00:00:01.250000Z",
        request=None,
        response=None,
        data=None,
        error=None,
    )
    assert j.duration_ms == 1250
