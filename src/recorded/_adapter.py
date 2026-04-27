"""Type adapter for slot values.

Three tiers, in priority order:

1. stdlib `dataclasses` — `Model(**data)` to validate, `dataclasses.asdict` to dump.
2. Pydantic v2 — auto-detected; uses `model_validate` / `model_dump`.
3. plain dict / primitive — accepted as-is, no validation.

A single `Adapter` class wraps one of these strategies. `model=None` means
"no model registered for this slot" — the value is passed through unchanged.

Pydantic is detected via duck-typing (`model_validate` + `model_dump` attrs).
We do not import pydantic; we rely on the user-supplied class carrying the
v2 protocol.
"""

from __future__ import annotations

import dataclasses
from typing import Any


def _is_pydantic_model(model: type) -> bool:
    return (
        isinstance(model, type)
        and hasattr(model, "model_validate")
        and hasattr(model, "model_dump")
    )


def _is_dataclass_type(model: type) -> bool:
    return isinstance(model, type) and dataclasses.is_dataclass(model)


class Adapter:
    """Serialize values *to* JSON-compatible primitives, deserialize back.

    `serialize(value)` accepts:
        - a model instance — validated by construction, dumped to dict
        - a dict — round-tripped through the model to validate (raises if invalid)
        - None — returned as None

    `deserialize(raw)` rebuilds a model instance from the stored dict.
    With `model=None`, both methods are pass-through.
    """

    __slots__ = ("model", "_kind")

    def __init__(self, model: type | None = None) -> None:
        self.model = model
        if model is None:
            self._kind = "passthrough"
        elif _is_pydantic_model(model):
            self._kind = "pydantic"
        elif _is_dataclass_type(model):
            self._kind = "dataclass"
        else:
            raise TypeError(
                f"Unsupported model type: {model!r}. "
                "Expected a dataclass, a Pydantic v2 BaseModel, or None."
            )

    def serialize(self, value: Any) -> Any:
        if value is None:
            return None
        if self._kind == "passthrough":
            return value
        if self._kind == "pydantic":
            if isinstance(value, self.model):  # type: ignore[arg-type]
                return value.model_dump(mode="json")
            # validate-then-dump: ensures stored shape is canonical
            return self.model.model_validate(value).model_dump(mode="json")  # type: ignore[union-attr]
        # dataclass
        if isinstance(value, self.model):  # type: ignore[arg-type]
            return dataclasses.asdict(value)
        if isinstance(value, dict):
            return dataclasses.asdict(self.model(**value))  # type: ignore[misc]
        raise TypeError(
            f"Cannot serialize {type(value).__name__} into "
            f"dataclass slot {self.model.__name__}"  # type: ignore[union-attr]
        )

    def deserialize(self, raw: Any) -> Any:
        if raw is None:
            return None
        if self._kind == "passthrough":
            return raw
        if self._kind == "pydantic":
            return self.model.model_validate(raw)  # type: ignore[union-attr]
        # dataclass
        return self.model(**raw)  # type: ignore[misc]
