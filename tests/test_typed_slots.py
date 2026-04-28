"""Typed-slot contract: round-trip safety preconditions.

The typed-slot contract refuses model shapes that can't round-trip
themselves. Today that is the Pydantic alias case: a model with field
aliases under default config writes canonical names but
`model_validate` demands aliases — rows would commit then fail to
rehydrate. Refused at decorator-evaluation time with a
`ConfigurationError` so misconfiguration surfaces before any call
runs.
"""

from __future__ import annotations

import pytest

import recorded
from recorded import recorder
from recorded._errors import ConfigurationError

pydantic = pytest.importorskip("pydantic")
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402


def test_pydantic_aliased_model_without_populate_by_name_raises_at_decorator(
    default_recorder,
):
    """Declaring `data=Model` with an aliased Pydantic model under
    default config raises at decorator evaluation — before any call
    runs."""

    class Aliased(BaseModel):
        canonical_name: str = Field(alias="aliased_name")

    with pytest.raises(ConfigurationError, match="populate_by_name"):

        @recorder(kind="t.alias.bad", data=Aliased)
        def fn(req):
            return {"canonical_name": req}


def test_pydantic_aliased_model_with_populate_by_name_round_trips(
    default_recorder,
):
    """Same model with `populate_by_name=True` round-trips cleanly:
    write the row, read it back, get the typed instance."""

    class AliasedOK(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        canonical_name: str = Field(alias="aliased_name")

    @recorder(kind="t.alias.ok", data=AliasedOK)
    def fn(req):
        # attach the canonical name so projection writes the canonical
        # shape, matching what model_dump() would have written.
        from recorded import attach

        attach("canonical_name", req)
        return {"ok": True}

    fn("value-x")

    job = recorded.last(1, kind="t.alias.ok")[0]
    # Rehydrated as the typed instance, no ValidationError.
    assert isinstance(job.data, AliasedOK)
    assert job.data.canonical_name == "value-x"


def test_pydantic_unaliased_model_passes_validation_unchanged(default_recorder):
    """The new check doesn't false-positive on plain (non-aliased)
    Pydantic models."""

    class Plain(BaseModel):
        x: int

    @recorder(kind="t.alias.plain", data=Plain)
    def fn(req):
        from recorded import attach

        attach("x", req)
        return {"ok": True}

    fn(7)
    job = recorded.last(1, kind="t.alias.plain")[0]
    assert isinstance(job.data, Plain)
    assert job.data.x == 7


def test_pydantic_validation_alias_without_populate_by_name_raises_at_decorator(
    default_recorder,
):
    """`Field(validation_alias=...)` alone (without `alias=...`) leaves
    `info.alias is None` but still makes `model_validate()` reject
    canonical-name input. The guard must catch it the same way it
    catches `alias=...`."""

    class ValidationAliased(BaseModel):
        canonical_name: str = Field(validation_alias="aliased_name")

    with pytest.raises(ConfigurationError, match="populate_by_name"):

        @recorder(kind="t.alias.validation.bad", data=ValidationAliased)
        def fn(req):
            return {"canonical_name": req}


def test_pydantic_serialization_alias_does_not_corrupt_storage(default_recorder):
    """`Field(serialization_alias=...)` plus
    `model_config(serialize_by_alias=True)` would normally cause
    `model_dump()` to emit aliased keys, breaking the canonical-shape
    contract. The adapter forces `by_alias=False` on dump everywhere it
    serializes Pydantic values, so the stored shape stays canonical and
    the matching `model_validate` on read can rehydrate it."""

    class SerAliased(BaseModel):
        model_config = ConfigDict(serialize_by_alias=True)
        canonical_name: str = Field(serialization_alias="aliased_name")

    @recorder(kind="t.alias.ser.ok", response=SerAliased, data=SerAliased)
    def fn(req):
        return SerAliased(canonical_name=req)

    fn("value-x")

    job = recorded.last(1, kind="t.alias.ser.ok")[0]
    # response slot rehydrated cleanly: stored shape was canonical,
    # not the aliased keys `serialize_by_alias=True` would have emitted.
    assert isinstance(job.response, SerAliased)
    assert job.response.canonical_name == "value-x"
    # data slot's projection of the response also stayed canonical.
    assert isinstance(job.data, SerAliased)
    assert job.data.canonical_name == "value-x"
