"""Type adapter for slot values.

Three tiers, in priority order:

1. stdlib `dataclasses` — `Model(**data)` to validate, `dataclasses.asdict` to dump.
2. Pydantic v2 — auto-detected; uses `model_validate` / `model_dump`.
3. plain dict / primitive — accepted as-is, no validation.

`Adapter` is an ABC with one subclass per tier. Construct via
`make_adapter(model)` rather than directly. `model=None` means "no model
registered for this slot" — the value is passed through unchanged.

Pydantic is detected via duck-typing (`model_validate` + `model_dump` attrs).
We do not import pydantic; we rely on the user-supplied class carrying the
v2 protocol.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from typing import Any

from ._errors import ConfigurationError, SerializationError


def _is_pydantic_model(model: type) -> bool:
    return (
        isinstance(model, type)
        and hasattr(model, "model_validate")
        and hasattr(model, "model_dump")
    )


def _is_dataclass_type(model: type) -> bool:
    return isinstance(model, type) and dataclasses.is_dataclass(model)


def _to_native(value: Any) -> Any:
    """Render a value into a JSON-native form when the slot has no model.

    Without this, returning a dataclass or Pydantic instance from a
    `@recorder` function with no `response=Model` declared would crash
    `json.dumps` and mark the (otherwise-successful) row failed —
    violating the audit invariant. With it, the natural-form return is
    recorded as its dump'd dict and the row stays `completed`.

    Detection is duck-typed (matches `_is_pydantic_model`'s precedent —
    we don't import pydantic). Containers of typed instances are not
    walked: a `list[Model]` return still requires the user to wrap in a
    response model with a typed list field. Top-level typed instances
    cover the realistic case.
    """
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


class Adapter(ABC):
    """Serialize/deserialize/project values for a typed slot.

    Subclasses correspond to the supported model kinds. Construct via
    `make_adapter(model)` rather than directly.

    `field_names` is the set of declared keys for this slot's model;
    `None` means "no schema, accept any key" (the passthrough case).
    `attach()` consults this to refuse undeclared keys at the call site
    against typed `data=` slots — declared keys are exactly the keys
    the rehydration path can reach.

    The adapter itself is slot-agnostic: it does not know which audit
    role (request/response/data/error) it has been mounted under.
    Call sites that already know the slot annotate `SerializationError`
    with `slot=` when re-raising — the adapter would otherwise need
    every construction site threading a slot literal through, for an
    attribute that's only meaningful at user-reachable raise sites.
    """

    model: type | None
    field_names: frozenset[str] | None

    @abstractmethod
    def serialize(self, value: Any) -> Any: ...

    @abstractmethod
    def deserialize(self, raw: Any) -> Any: ...

    @abstractmethod
    def project(self, response: Any) -> dict[str, Any]:
        """Best-effort response → queryable-data dict.

        Returns `{}` when the response shape doesn't match the slot's
        model. Raising is reserved for hard bugs; the caller catches and
        falls back to `{}` (with a warning if `warn_on_data_drift` is on).
        """


class _PassthroughAdapter(Adapter):
    """No model registered — pass values through, render natively for storage."""

    model: type | None = None
    field_names: frozenset[str] | None = None

    def serialize(self, value: Any) -> Any:
        if value is None:
            return None
        return _to_native(value)

    def deserialize(self, raw: Any) -> Any:
        return raw

    def project(self, response: Any) -> dict[str, Any]:
        return {}


class _PydanticAdapter(Adapter):
    """Pydantic v2 (duck-typed) — `model_validate` / `model_dump`.

    `model` is typed as `type[Any]` rather than `type` because the v2
    protocol attributes (`model_validate`, `model_dump`) are on the
    user's class but not on the bare `type` metaclass — we don't import
    pydantic, so we widen to `type[Any]` to let attribute access through.
    """

    model: type[Any]
    field_names: frozenset[str]

    def __init__(self, model: type) -> None:
        self.model = model
        # `model_fields.keys()` returns canonical field names (not
        # aliases). That is the right set for `attach()` validation
        # because the stored shape is keyed by canonical names —
        # an attach by alias would land in a key the rehydration path
        # cannot reach. Access via `self.model` (typed `type[Any]`) so
        # ty doesn't complain about `model_fields` not being on bare
        # `type` — same dodge as `model_validate` / `model_dump` below.
        self.field_names = frozenset(self.model.model_fields.keys())

        # Round-trip safety: a model with field aliases under default
        # Pydantic config writes canonical names via `model_dump()` but
        # demands aliases at `model_validate()`. Rows would write
        # successfully then fail to rehydrate — exactly the asymmetry
        # the typed-slot contract is meant to refuse.
        # (`self.model` for the same `type[Any]` reason as above.)
        aliased_fields = [
            name for name, info in self.model.model_fields.items() if info.alias is not None
        ]
        if aliased_fields:
            config = getattr(self.model, "model_config", {})
            if isinstance(config, dict):
                populate_by_name = config.get("populate_by_name", False)
                # Pydantic v2.11+ also exposes `validate_by_name` as an
                # opt-in to accept canonical names; either is sufficient
                # to make round-trip safe.
                validate_by_name = config.get("validate_by_name", False)
            else:
                populate_by_name = getattr(config, "populate_by_name", False)
                validate_by_name = getattr(config, "validate_by_name", False)
            if not (populate_by_name or validate_by_name):
                raise ConfigurationError(
                    f"{self.model.__name__} declares field aliases "
                    f"({aliased_fields!r}) without populate_by_name=True. "
                    "With default Pydantic config, model_dump() writes "
                    "canonical field names but model_validate() expects "
                    "aliases — rows would be unreadable. Set "
                    "model_config = ConfigDict(populate_by_name=True), "
                    "or remove the field aliases."
                )

    def serialize(self, value: Any) -> Any:
        if value is None:
            return None
        try:
            if isinstance(value, self.model):
                return value.model_dump(mode="json")
            # validate-then-dump: ensures stored shape is canonical
            return self.model.model_validate(value).model_dump(mode="json")
        except Exception as exc:
            raise SerializationError(
                f"Cannot serialize {type(value).__name__} into "
                f"pydantic slot {self.model.__name__}: {exc}",
                model=self.model,
                value=value,
            ) from exc

    def deserialize(self, raw: Any) -> Any:
        if raw is None:
            return None
        return self.model.model_validate(raw)

    def project(self, response: Any) -> dict[str, Any]:
        if isinstance(response, self.model):
            return response.model_dump(mode="json")
        if isinstance(response, dict):
            return self.model.model_validate(response).model_dump(mode="json")
        return {}


class _DataclassAdapter(Adapter):
    """stdlib dataclass — `Model(**dict)` / `dataclasses.asdict`."""

    model: type
    field_names: frozenset[str]

    def __init__(self, model: type) -> None:
        self.model = model
        self.field_names = frozenset(f.name for f in dataclasses.fields(model))

    def serialize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, self.model):
            return dataclasses.asdict(value)
        if isinstance(value, dict):
            try:
                return dataclasses.asdict(self.model(**value))
            except TypeError as exc:
                raise SerializationError(
                    f"Cannot construct {self.model.__name__} from dict: {exc}",
                    model=self.model,
                    value=value,
                ) from exc
        raise SerializationError(
            f"Cannot serialize {type(value).__name__} into dataclass slot {self.model.__name__}",
            model=self.model,
            value=value,
        )

    def deserialize(self, raw: Any) -> Any:
        if raw is None:
            return None
        return self.model(**raw)

    def project(self, response: Any) -> dict[str, Any]:
        if isinstance(response, self.model):
            return dataclasses.asdict(response)
        if isinstance(response, dict):
            filtered = {k: v for k, v in response.items() if k in self.field_names}
            return dataclasses.asdict(self.model(**filtered))
        return {}


def make_adapter(model: type | None = None) -> Adapter:
    """Pick the right adapter for the model type.

    - `model=None` → `_PassthroughAdapter()`
    - pydantic v2-shaped (duck-typed) → `_PydanticAdapter(model)`
    - dataclass type → `_DataclassAdapter(model)`
    - anything else → `ConfigurationError`
    """
    if model is None:
        return _PassthroughAdapter()
    if _is_pydantic_model(model):
        return _PydanticAdapter(model)
    if _is_dataclass_type(model):
        return _DataclassAdapter(model)
    raise ConfigurationError(
        f"Unsupported model type: {model!r}. "
        "Expected a dataclass, a Pydantic v2 BaseModel, or None."
    )
