"""Recording machinery shared between bare-call and worker.

Both bare-call (sync and async wrappers in `_decorator`) and the worker
(`_worker._execute`) delegate into this layer for the universal
serialize-validate-record flow. Holds the helpers that don't belong in
`_decorator` (which is the call-site spine) or `_worker` (which is the
event-loop driver).

Contents:
- `make_error_json`: single source of truth for `{type, message}` JSON.
- `_validate_call_args`: refuse-to-compile rules at call time.
- `_capture_request`, `_serialize_request`: request envelope + JSON.
- `_serialize_error`, `_serialize_recording_failure`: error envelope + JSON.
- `_write_completion`, `_build_data_json`, `_project_response`: success-path
  recording (response + data projection + attach buffer flush).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import is_dataclass
from typing import TYPE_CHECKING, Any

from . import _registry, _storage
from ._context import _UNSET, current_job
from ._errors import ConfigurationError

if TYPE_CHECKING:
    from ._recorder import Recorder


# ---------------------------------------------------------------------------
# Error JSON shape
# ---------------------------------------------------------------------------


def make_error_json(type_: str, message: str) -> str:
    """Single source of truth for the `{type, message}` JSON shape used by
    every error writer (`_serialize_error`, `_serialize_recording_failure`,
    the worker's cancel marker, the unknown-kind marker, the reaper).

    Consumers like `JoinedSiblingFailedError` read `.message` to format
    their string; any other key is silently dropped.
    """
    return json.dumps({"type": type_, "message": message})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_call_args(
    entry: _registry.RegistryEntry,
    key: str | None,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    *,
    submit: bool = False,
) -> None:
    """Call-time validation of decorator + call-shape combinations.

    Three checks, all refuse-to-compile rather than silently mis-record:

    1. `key=` against an auto-derived kind. The default kind comes from
       `f"{module}.{qualname}"` and silently changes when the function
       is renamed or moved — exactly the failure mode idempotency was
       meant to prevent.

    2. `request=Model` against a multi-argument call shape. The capture
       envelope for multi-arg / kwarg calls is `{args, kwargs}` — that
       shape can't be validated through a typed request model. Mirrors
       the `key=` + auto-kind precedent: misuse caught at call time, not
       silently mis-recorded.

    3. `.submit()` against a multi-argument call shape (with or without
       `request=Model`). The worker reconstructs the call from the stored
       request envelope; round-tripping `{args, kwargs}` and unpacking
       it generically can collide with naturally-shaped dicts. Multi-arg
       `.submit()` requires a `request=Model` that defines the call's
       shape, OR use the bare-call form (which has the live args and
       never round-trips).
    """
    if key is not None and entry.auto_kind:
        raise ConfigurationError(
            f"{entry.kind} uses an auto-derived kind (\"{entry.kind}\"). "
            "Idempotency keys require an explicit kind to remain stable across "
            "renames. Add: @recorder(kind=\"...\")"
        )
    if entry.request.model is not None:
        kw = kwargs or {}
        if len(args) != 1 or kw:
            raise ConfigurationError(
                f"{entry.kind}: request=Model requires single-positional-arg "
                f"call shape; got args={len(args)}, kwargs={list(kw)}. "
                "Either drop request=Model or simplify the call shape."
            )
    if submit:
        kw = kwargs or {}
        if len(args) != 1 or kw:
            raise ConfigurationError(
                f"{entry.kind}: .submit() requires single-positional-arg "
                f"call shape; got args={len(args)}, kwargs={list(kw)}. "
                "Wrap multi-arg signatures with request=Model so the worker "
                "can reconstruct the call, or use the bare call form."
            )


# ---------------------------------------------------------------------------
# Request capture + serialization
# ---------------------------------------------------------------------------


def _capture_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Single positional arg → that arg. Otherwise → {args, kwargs} envelope."""
    if len(args) == 1 and not kwargs:
        return args[0]
    return {"args": list(args), "kwargs": kwargs}


def _serialize_request(entry: _registry.RegistryEntry, captured: Any) -> str | None:
    if captured is None:
        return None
    serialized = entry.request.serialize(captured)
    return json.dumps(serialized)


# ---------------------------------------------------------------------------
# Error serialization
# ---------------------------------------------------------------------------


def _serialize_error(entry: _registry.RegistryEntry, exc: BaseException) -> str:
    """Compose the JSON payload written to `error_json`.

    Default shape: `{type: <ExcClass>, message: <str(exc)>}` derived from
    the live exception. If the wrapped function called `attach_error(...)`
    on the current `JobContext` AND the decorator was registered with an
    `error=Model`, the attached payload is validated through that adapter
    and used instead — this is the `error=Model` wiring promised by the
    decorator parameter.

    The original exception still propagates verbatim to the caller —
    `attach_error` only re-shapes the *recording*. If the attached payload
    fails validation through the adapter we fall back to the default
    `{type, message}` so a serialization bug never silently drops the
    record.
    """
    ctx = current_job.get()
    if (
        ctx is not None
        and ctx.error_buffer is not _UNSET
        and entry.error._kind != "passthrough"
    ):
        try:
            return json.dumps(entry.error.serialize(ctx.error_buffer))
        except Exception:
            # Fall through to {type, message} of the *original* exc rather
            # than masking the underlying failure. Caller still sees `exc`.
            pass
    return make_error_json(type(exc).__name__, str(exc))


def _serialize_recording_failure(exc: BaseException) -> str:
    """Error JSON for the case where the wrapped function succeeded but
    the recorder couldn't serialize its result."""
    return make_error_json(
        type(exc).__name__,
        f"recorder failed to serialize response: {exc}",
    )


# ---------------------------------------------------------------------------
# Success-path recording: response, data projection, attach buffer
# ---------------------------------------------------------------------------


def _write_completion(
    recorder_inst: "Recorder",
    entry: _registry.RegistryEntry,
    job_id: str,
    result: Any,
    buffer: dict[str, Any],
) -> None:
    response_json = (
        None
        if result is None
        else json.dumps(entry.response.serialize(result))
    )
    data_json = _build_data_json(entry, result, buffer)
    recorder_inst._mark_completed(job_id, _storage.now_iso(), response_json, data_json)


def _build_data_json(
    entry: _registry.RegistryEntry, response: Any, buffer: dict[str, Any]
) -> str | None:
    """Compose `data_json` from optional projection + attach buffer.

    Conflict rule (DESIGN.md): the projection populates initial keys;
    attaches merge in last-write-wins.

    Projection-from-response is best-effort. The supported source shapes
    (driven by `entry.data._kind`):

    - dataclass-typed slot, dict response: validate by construction, dump.
    - dataclass-typed slot, instance of `data` model: `dataclasses.asdict`.
    - dataclass-typed slot, *some other* dataclass instance: dump it,
      filter to `data` model's declared field names.
    - pydantic-typed slot, dict response: `model_validate(...).model_dump(mode="json")`.
    - pydantic-typed slot, instance of `data` model: `model_dump(mode="json")`.
    - pydantic-typed slot, *some other* pydantic instance: `model_dump(mode="json")`,
      filter to `data` model's declared field names.

    Anything else falls through with no projection, leaving only the
    attach buffer (which may be empty). The full response is still in
    `response_json`; data is the queryable projection.
    """
    projected: dict[str, Any] = {}
    model = entry.data.model
    if model is not None:
        try:
            projected = _project_response(entry.data._kind, model, response)
        except Exception:
            # Projection is opportunistic — a partial response is still recorded
            # via response_json; we just skip the projected slice.
            projected = {}

    merged = {**projected, **buffer}
    if not merged:
        return None
    return json.dumps(merged)


def _project_response(kind: str, model: type, response: Any) -> dict[str, Any]:
    """Generalized response → dict projection driven by adapter `_kind`.

    Returns an empty dict for unprojectable shapes; raises only for hard
    bugs (the caller catches and falls back to `{}`).
    """
    if kind == "dataclass":
        if is_dataclass(model):
            names = {f.name for f in dataclasses.fields(model)}
            if isinstance(response, model):
                return dataclasses.asdict(response)
            if isinstance(response, dict):
                filtered = {k: v for k, v in response.items() if k in names}
                return dataclasses.asdict(model(**filtered))
            if dataclasses.is_dataclass(response) and not isinstance(response, type):
                dumped = dataclasses.asdict(response)
                return {k: v for k, v in dumped.items() if k in names}
        return {}

    if kind == "pydantic":
        # `_kind == "pydantic"` is set by Adapter only when the model
        # implements `model_validate` + `model_dump`.
        names = set(getattr(model, "model_fields", {}).keys())
        if isinstance(response, model):
            return response.model_dump(mode="json")
        if isinstance(response, dict):
            return model.model_validate(response).model_dump(mode="json")
        if hasattr(response, "model_dump"):
            dumped = response.model_dump(mode="json")
            if names:
                return {k: v for k, v in dumped.items() if k in names}
            return dumped
        return {}

    return {}
