"""`data=Model` projection: instance-of / dict-validate, drift warning.

B'' (adapter/projector-refactor) removed the cross-shape "by-name field
match" projection branch. Auto-projection now requires the response to
be either an instance of the declared `data` model OR a dict that
validates against it. Anything else falls through to `{}`; if
`warn_on_data_drift` is on, the drift surfaces as a deduped warning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import pytest

from recorded import Recorder, recorder
from recorded import _recorder as _recorder_mod
from recorded._handle import JobHandle

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


# --- supported-source-shape tests -----------------------------------------


def test_data_projection_dataclass_instance(default_recorder):
    """Returning an instance of the declared dataclass model projects to data."""

    @recorder(kind="t.proj.dc", data=OrderViewDc)
    def fn(req):
        return OrderViewDc(order_id="o1", total=12.5)

    fn({})

    rows = default_recorder.last(1, kind="t.proj.dc")
    job = rows[0]
    assert isinstance(job.data, OrderViewDc)
    assert job.data.order_id == "o1"
    assert job.data.total == 12.5

    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE id=?", (job.id,)
    )
    parsed = json.loads(raw[0])
    assert parsed == {"order_id": "o1", "total": 12.5}


def test_data_projection_pydantic_response_instance(default_recorder):
    """Returning an instance of the declared pydantic model projects to data."""

    @recorder(kind="t.proj.pyd_inst", data=OrderViewPyd)
    def fn(req):
        return OrderViewPyd(order_id="o2", total=99.0)

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
    """Dict response that validates against the data model still projects."""

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


def test_data_projection_dataclass_dict_validates(default_recorder):
    """Dict response with a dataclass `data` model: filter to declared fields, construct."""

    @recorder(kind="t.proj.dc_dict", data=OrderViewDc)
    def fn(req):
        return {"order_id": "o4", "total": 2.0, "extra": "drop me"}

    fn({})

    rows = default_recorder.last(1, kind="t.proj.dc_dict")
    job = rows[0]
    assert isinstance(job.data, OrderViewDc)
    assert job.data.order_id == "o4"
    assert job.data.total == 2.0


# --- drift-warning tests --------------------------------------------------


@pytest.fixture
def drift_caplog(caplog):
    """Capture WARNING-level logs from the `recorded` logger."""
    caplog.set_level(logging.WARNING, logger="recorded")
    return caplog


def _drift_warnings(caplog):
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "projection produced empty data" in r.getMessage()
    ]


def test_drift_warning_emitted_once_per_kind_reason(default_recorder, drift_caplog):
    """Drift warning fires once per (kind, reason) per Recorder."""

    @recorder(kind="t.drift.shape", data=OrderViewPyd)
    def fn(req):
        return {"unrelated": "values"}  # validates? no — missing required fields

    # Wait — this dict path: model_validate raises (missing required fields).
    # That makes reason = projection_raised. Use a primitive instead for
    # pure shape_mismatch.

    @recorder(kind="t.drift.shape2", data=OrderViewPyd)
    def fn2(req):
        return 42  # primitive — pure shape_mismatch

    fn2({})
    fn2({})  # second call — no second warning
    fn2({})

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.drift.shape2" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "shape_mismatch" not in warnings[0].getMessage()  # reason isn't surfaced verbatim
    assert "OrderViewPyd" in warnings[0].getMessage()


def test_drift_warning_silenced_when_attach_populates(default_recorder, drift_caplog):
    """attach() populates data → merged is non-empty → no drift warning."""
    from recorded import attach

    @recorder(kind="t.drift.attach", data=OrderViewPyd)
    def fn(req):
        attach("customer", "c1")
        return 42  # would normally drift

    fn({})

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.drift.attach" in r.getMessage()
    ]
    assert warnings == []

    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE kind=?", ("t.drift.attach",)
    )
    assert json.loads(raw[0]) == {"customer": "c1"}


def test_drift_warning_disabled_via_recorder_flag(db_path, drift_caplog):
    """Recorder(warn_on_data_drift=False) suppresses the warning."""
    r = Recorder(path=db_path, warn_on_data_drift=False)
    _recorder_mod._set_default(r)
    try:

        @recorder(kind="t.drift.disabled", data=OrderViewPyd)
        def fn(req):
            return 42

        fn({})

        warnings = [
            r for r in _drift_warnings(drift_caplog)
            if "t.drift.disabled" in r.getMessage()
        ]
        assert warnings == []
    finally:
        _recorder_mod._set_default(None)
        r.shutdown()


def test_cross_shape_projection_no_longer_fires(default_recorder, drift_caplog):
    """Regression test for B'': cross-shape match no longer projects.

    Previously, returning a model with overlapping field names would
    populate data_json via the "filter by name" branch. Now data_json is
    empty and a drift warning fires.
    """

    @dataclass
    class FullResponseDc:
        order_id: str
        total: float
        ignored_field: str

    @recorder(kind="t.cross.dc", response=FullResponseDc, data=OrderViewDc)
    def fn(req):
        return FullResponseDc(order_id="o1", total=12.5, ignored_field="ignored")

    fn({})

    rows = default_recorder.last(1, kind="t.cross.dc")
    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE id=?", (rows[0].id,)
    )
    assert raw[0] is None  # cross-shape no longer projects

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.cross.dc" in r.getMessage()
    ]
    assert len(warnings) == 1


# --- weird-shape edge cases -----------------------------------------------


def test_returning_none_does_not_warn(default_recorder, drift_caplog):
    """Response is None → projection isn't expected to fire → no warning."""

    @recorder(kind="t.drift.none", data=OrderViewPyd)
    def fn(req):
        return None

    fn({})

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.drift.none" in r.getMessage()
    ]
    assert warnings == []


@pytest.mark.parametrize("primitive", [42, "string", True, 3.14])
def test_returning_primitive_warns(default_recorder, drift_caplog, primitive):
    """Returning a primitive when data=Model is declared → drift warning."""
    kind = f"t.drift.prim.{type(primitive).__name__}"

    @recorder(kind=kind, data=OrderViewPyd)
    def fn(req):
        return primitive

    fn({})

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if kind in r.getMessage()
    ]
    assert len(warnings) == 1


def test_subclass_of_data_model_projects_no_warning(default_recorder, drift_caplog):
    """Returning a subclass instance: isinstance matches → projection fires."""

    class OrderViewExtended(OrderViewPyd):
        bonus: str = "x"

    @recorder(kind="t.subclass", data=OrderViewPyd)
    def fn(req):
        return OrderViewExtended(order_id="o1", total=1.0, bonus="extra")

    fn({})

    rows = default_recorder.last(1, kind="t.subclass")
    job = rows[0]
    assert isinstance(job.data, OrderViewPyd)

    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE id=?", (job.id,)
    )
    parsed = json.loads(raw[0])
    # The subclass dump may include `bonus` (pydantic dumps all declared fields).
    # The point: projection fires, no warning.
    assert parsed["order_id"] == "o1"
    assert parsed["total"] == 1.0

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.subclass" in r.getMessage()
    ]
    assert warnings == []


def test_dict_with_extra_fields_pydantic_default_behavior(
    default_recorder, drift_caplog
):
    """Dict response with extra fields beyond the declared pydantic model.

    Documents pydantic v2's default: extras are ignored on `model_validate`.
    Projection succeeds with just the declared fields; no warning fires.
    """

    @recorder(kind="t.extras", data=OrderViewPyd)
    def fn(req):
        return {"order_id": "o1", "total": 1.0, "extra": "ignored"}

    fn({})

    rows = default_recorder.last(1, kind="t.extras")
    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE id=?", (rows[0].id,)
    )
    parsed = json.loads(raw[0])
    assert parsed == {"order_id": "o1", "total": 1.0}

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.extras" in r.getMessage()
    ]
    assert warnings == []


def test_dict_missing_required_fields_warns_with_projection_raised(
    default_recorder, drift_caplog
):
    """Dict missing required fields → model_validate raises → projection_raised reason."""

    @recorder(kind="t.missing", data=OrderViewPyd)
    def fn(req):
        return {"order_id": "o1"}  # missing `total`

    fn({})

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.missing" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "projection raised" in warnings[0].getMessage()


def test_attach_and_projection_both_populate_last_write_wins(
    default_recorder, drift_caplog
):
    """Both attach and projection populate; conflict rule: attach wins (last-write)."""
    from recorded import attach

    @recorder(kind="t.merge", data=OrderViewPyd)
    def fn(req):
        attach("order_id", "from_attach")  # collides with projection key
        attach("separate_key", "x")  # non-colliding
        return OrderViewPyd(order_id="from_response", total=42.0)

    fn({})

    rows = default_recorder.last(1, kind="t.merge")
    raw = default_recorder._fetchone(
        "SELECT data_json FROM jobs WHERE id=?", (rows[0].id,)
    )
    parsed = json.loads(raw[0])
    # Attach (last-write) wins on the colliding key.
    assert parsed["order_id"] == "from_attach"
    assert parsed["total"] == 42.0  # from projection
    assert parsed["separate_key"] == "x"  # from attach

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.merge" in r.getMessage()
    ]
    assert warnings == []


def test_drift_via_submit_path(recorder_for_submit, drift_caplog):
    """Worker-driven path (.submit) produces identical drift warning behavior."""

    @recorder(kind="t.drift.submit", data=OrderViewPyd)
    def fn(req):
        return 42

    handle: JobHandle = fn.submit({})
    handle.wait_sync(timeout=5.0)

    warnings = [
        r for r in _drift_warnings(drift_caplog)
        if "t.drift.submit" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_per_recorder_dedup_no_cross_contamination(db_path, drift_caplog):
    """Two separate Recorders with the same kind both warn independently."""

    @recorder(kind="t.dedup.iso", data=OrderViewPyd)
    def fn(req):
        return 42

    r1 = Recorder(path=db_path)
    _recorder_mod._set_default(r1)
    try:
        fn({})
        fn({})  # second call same recorder — silent
    finally:
        _recorder_mod._set_default(None)
        r1.shutdown()

    warnings_r1 = [
        r for r in _drift_warnings(drift_caplog)
        if "t.dedup.iso" in r.getMessage()
    ]
    assert len(warnings_r1) == 1

    drift_caplog.clear()

    r2 = Recorder(path=db_path)
    _recorder_mod._set_default(r2)
    try:
        fn({})  # fresh Recorder — should warn again
    finally:
        _recorder_mod._set_default(None)
        r2.shutdown()

    warnings_r2 = [
        r for r in _drift_warnings(drift_caplog)
        if "t.dedup.iso" in r.getMessage()
    ]
    assert len(warnings_r2) == 1


def test_configure_warn_on_data_drift_forwarded(monkeypatch, db_path, drift_caplog):
    """configure(warn_on_data_drift=False) forwards into the Recorder."""
    import recorded as recorded_pkg

    # Reset the configured singleton.
    monkeypatch.setattr(_recorder_mod, "_default", None)

    r = recorded_pkg.configure(path=db_path, warn_on_data_drift=False)
    try:
        assert r.warn_on_data_drift is False

        @recorder(kind="t.configure.disabled", data=OrderViewPyd)
        def fn(req):
            return 42

        fn({})

        warnings = [
            rec for rec in _drift_warnings(drift_caplog)
            if "t.configure.disabled" in rec.getMessage()
        ]
        assert warnings == []
    finally:
        monkeypatch.setattr(_recorder_mod, "_default", None)
        r.shutdown()


# --- fixture for submit path ----------------------------------------------


@pytest.fixture
def recorder_for_submit(db_path):
    """A Recorder installed as default that supports .submit() (worker path)."""
    r = Recorder(path=db_path)
    _recorder_mod._set_default(r)
    try:
        yield r
    finally:
        _recorder_mod._set_default(None)
        r.shutdown()
