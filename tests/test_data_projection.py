"""`data=Model` projection generalization (Stream C 2.C.2).

Phase 1 / Stream A only projected when the response was a `dict`.
Stream C generalizes to dataclass and Pydantic instances as well; the
existing dict-source path must continue to work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from recorded import recorder

pydantic = pytest.importorskip("pydantic")
from pydantic import BaseModel  # noqa: E402


# --- shared shapes ---------------------------------------------------------


@dataclass
class OrderViewDc:
    order_id: str
    total: float


class OrderViewPyd(BaseModel):
    order_id: str
    total: float


# --- tests -----------------------------------------------------------------


def test_data_projection_dataclass_response(default_recorder):
    """`data=DataclassView` + dataclass response → projected `data_json`.

    The response is a *different* dataclass than the projection model, to
    exercise "filter to data-model's declared field names" generalization.
    """

    @dataclass
    class FullResponse:
        order_id: str
        total: float
        ignored_field: str

    @recorder(kind="t.proj.dc", response=FullResponse, data=OrderViewDc)
    def fn(req):
        return FullResponse(order_id="o1", total=12.5, ignored_field="ignored")

    fn({})

    rows = default_recorder.last(1, kind="t.proj.dc")
    job = rows[0]
    # `data` is rehydrated as the projection model (dataclass).
    assert isinstance(job.data, OrderViewDc)
    assert job.data.order_id == "o1"
    assert job.data.total == 12.5

    # The raw `data_json` should not contain the ignored field.
    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE id=?", (job.id,)
    )
    parsed = json.loads(raw[0])
    assert parsed == {"order_id": "o1", "total": 12.5}
    assert "ignored_field" not in parsed


def test_data_projection_pydantic_response_instance(default_recorder):
    """`data=PydModel` + pydantic instance response → projected `data_json`."""

    class FullResponseM(BaseModel):
        order_id: str
        total: float
        ignored_field: str

    @recorder(kind="t.proj.pyd_inst", response=FullResponseM, data=OrderViewPyd)
    def fn(req):
        return FullResponseM(order_id="o2", total=99.0, ignored_field="x")

    fn({})

    rows = default_recorder.last(1, kind="t.proj.pyd_inst")
    job = rows[0]
    assert isinstance(job.data, OrderViewPyd)
    assert job.data.order_id == "o2"
    assert job.data.total == 99.0

    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE id=?", (job.id,)
    )
    parsed = json.loads(raw[0])
    assert parsed == {"order_id": "o2", "total": 99.0}


def test_data_projection_pydantic_response_dict_still_works(default_recorder):
    """The phase-1 dict-source projection path still works."""

    @recorder(kind="t.proj.pyd_dict", data=OrderViewPyd)
    def fn(req):
        return {"order_id": "o3", "total": 1.0, "extra": "drop me"}

    fn({})

    rows = default_recorder.last(1, kind="t.proj.pyd_dict")
    job = rows[0]
    assert isinstance(job.data, OrderViewPyd)
    assert job.data.order_id == "o3"
    assert job.data.total == 1.0

    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE id=?", (job.id,)
    )
    parsed = json.loads(raw[0])
    # `model_validate` strips unknown fields by default; `extra` doesn't survive.
    assert parsed == {"order_id": "o3", "total": 1.0}
