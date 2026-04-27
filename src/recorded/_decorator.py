"""`@recorder` decorator + bare-call lifecycle.

Three pieces, woven together:

1. Decoration registers metadata in `_registry` and returns a wrapper
   that preserves the wrapped function's calling convention (sync stays
   sync, async stays async).
2. Each call drives the three-write lifecycle inline (no worker):
   `insert pending → update running → execute → update terminal`.
3. The `key=` / `retry_failed=` kwargs are reserved at every call surface
   and stripped from what we forward to the wrapped function.

Cross-mode shims (`.sync`, `.async_run`) and full idempotency live in
sibling helpers; this file is the spine.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import inspect
import json
import sqlite3
from dataclasses import is_dataclass
from typing import Any, Callable

from . import _registry, _storage
from ._adapter import Adapter
from ._context import JobContext, current_job
from ._errors import (
    ConfigurationError,
    IdempotencyRaceError,
    JoinedSiblingFailedError,
    JoinTimeoutError,
    SerializationError,
    SyncInLoopError,
)
from ._recorder import (
    DEFAULT_JOIN_TIMEOUT_S,
    NOTIFY_POLL_INTERVAL_S,
    Recorder,
    get_default,
)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def recorder(
    fn: Callable[..., Any] | None = None,
    *,
    kind: str | None = None,
    request: type | None = None,
    response: type | None = None,
    data: type | None = None,
    error: type | None = None,
) -> Any:
    """Wrap a function so each call records its lifecycle and result.

    Usable bare (`@recorder`) or as a factory (`@recorder(kind="...", ...)`).
    The wrapper preserves the wrapped function's calling convention.
    """

    def decorate(fn: Callable[..., Any]) -> Any:
        auto_kind = kind is None
        actual_kind = kind or f"{fn.__module__}.{fn.__qualname__}"

        entry = _registry.RegistryEntry(
            kind=actual_kind,
            request=Adapter(request),
            response=Adapter(response),
            data=Adapter(data),
            error=Adapter(error),
            fn=fn,
            auto_kind=auto_kind,
        )
        _registry.register(entry)

        if inspect.iscoroutinefunction(fn):
            wrapper = _build_async_wrapper(fn, entry)
        else:
            wrapper = _build_sync_wrapper(fn, entry)

        wrapper.kind = actual_kind  # type: ignore[attr-defined]
        wrapper._entry = entry  # type: ignore[attr-defined]
        wrapper._is_async = inspect.iscoroutinefunction(fn)  # type: ignore[attr-defined]
        return wrapper

    if fn is not None:
        return decorate(fn)
    return decorate


# ---------------------------------------------------------------------------
# Wrapper builders
# ---------------------------------------------------------------------------


def _build_async_wrapper(
    fn: Callable[..., Any], entry: _registry.RegistryEntry
) -> Callable[..., Any]:
    @functools.wraps(fn)
    async def async_wrapper(
        *args: Any,
        key: str | None = None,
        retry_failed: bool = True,
        **kwargs: Any,
    ) -> Any:
        _validate_call_args(entry, key)

        recorder_inst = get_default()

        if key is not None:
            joined = await _async_try_join_existing(
                recorder_inst, entry, key, retry_failed
            )
            if joined is not _NO_JOIN:
                return joined

        captured_request = _capture_request(args, kwargs)
        request_json = _serialize_request(entry, captured_request)
        job_id = _storage.new_id()
        submitted_at = _storage.now_iso()

        try:
            await asyncio.to_thread(
                recorder_inst._insert_pending,
                job_id,
                entry.kind,
                key,
                submitted_at,
                request_json,
            )
        except sqlite3.IntegrityError:
            # Lost the race against another caller after our pre-check;
            # fold into the existing row.
            return await _async_wait_for_join(
                recorder_inst, entry, key, retry_failed
            )

        ctx = JobContext(job_id=job_id, kind=entry.kind, recorder=recorder_inst)
        token = current_job.set(ctx)
        try:
            await asyncio.to_thread(
                recorder_inst._mark_running, job_id, _storage.now_iso()
            )
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                await asyncio.to_thread(
                    recorder_inst._mark_failed,
                    job_id,
                    _storage.now_iso(),
                    _serialize_error(entry, exc),
                )
                raise
            try:
                await asyncio.to_thread(
                    _write_completion,
                    recorder_inst,
                    entry,
                    job_id,
                    result,
                    ctx.buffer,
                )
            except Exception as exc:
                # Wrapped function succeeded, but we can't record its result
                # (e.g. response is not JSON-serializable). Mark the row failed
                # rather than leak it as `running`; phase-2 reaper would
                # recover it but we don't have one yet.
                await asyncio.to_thread(
                    recorder_inst._mark_failed,
                    job_id,
                    _storage.now_iso(),
                    _serialize_recording_failure(exc),
                )
                raise SerializationError(
                    f"recorder failed to serialize response from "
                    f"{fn.__qualname__}: {exc}",
                    slot="response",
                    value=result,
                ) from exc
            return result
        finally:
            current_job.reset(token)

    _attach_call_modes(async_wrapper, fn, entry, is_async=True)
    return async_wrapper


def _build_sync_wrapper(
    fn: Callable[..., Any], entry: _registry.RegistryEntry
) -> Callable[..., Any]:
    @functools.wraps(fn)
    def sync_wrapper(
        *args: Any,
        key: str | None = None,
        retry_failed: bool = True,
        **kwargs: Any,
    ) -> Any:
        _validate_call_args(entry, key)

        recorder_inst = get_default()

        if key is not None:
            joined = _try_join_existing(recorder_inst, entry, key, retry_failed)
            if joined is not _NO_JOIN:
                return joined

        captured_request = _capture_request(args, kwargs)
        request_json = _serialize_request(entry, captured_request)
        job_id = _storage.new_id()
        submitted_at = _storage.now_iso()

        try:
            recorder_inst._insert_pending(
                job_id, entry.kind, key, submitted_at, request_json
            )
        except sqlite3.IntegrityError:
            return _wait_for_join(recorder_inst, entry, key, retry_failed)

        ctx = JobContext(job_id=job_id, kind=entry.kind, recorder=recorder_inst)
        token = current_job.set(ctx)
        try:
            recorder_inst._mark_running(job_id, _storage.now_iso())
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                recorder_inst._mark_failed(
                    job_id, _storage.now_iso(), _serialize_error(entry, exc)
                )
                raise
            try:
                _write_completion(recorder_inst, entry, job_id, result, ctx.buffer)
            except Exception as exc:
                # See async_wrapper: avoid stuck-running on unrecordable response.
                recorder_inst._mark_failed(
                    job_id, _storage.now_iso(), _serialize_recording_failure(exc)
                )
                raise SerializationError(
                    f"recorder failed to serialize response from "
                    f"{fn.__qualname__}: {exc}",
                    slot="response",
                    value=result,
                ) from exc
            return result
        finally:
            current_job.reset(token)

    _attach_call_modes(sync_wrapper, fn, entry, is_async=False)
    return sync_wrapper


# ---------------------------------------------------------------------------
# Cross-mode shims + .submit stub
# ---------------------------------------------------------------------------


def _attach_call_modes(
    wrapper: Callable[..., Any],
    fn: Callable[..., Any],
    entry: _registry.RegistryEntry,
    *,
    is_async: bool,
) -> None:
    """Attach `.sync`, `.async_run`, `.submit` to the wrapper.

    Per the brief, `.submit` raises `NotImplementedError` in phase 1 —
    the worker lands in phase 2.
    """

    if is_async:
        def _sync(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise SyncInLoopError(
                    f"{fn.__qualname__}.sync() was called from inside a running "
                    "event loop. Use `await place_order(req)` directly from the "
                    "async context, or move the call to a synchronous entry point."
                )
            return asyncio.run(wrapper(*args, **kwargs))

        async def _async_run(*args: Any, **kwargs: Any) -> Any:
            # Already async; just await the wrapper.
            return await wrapper(*args, **kwargs)

        wrapper.sync = _sync  # type: ignore[attr-defined]
        wrapper.async_run = _async_run  # type: ignore[attr-defined]
    else:
        def _sync(*args: Any, **kwargs: Any) -> Any:
            # Already sync; just call the wrapper.
            return wrapper(*args, **kwargs)

        async def _async_run(*args: Any, **kwargs: Any) -> Any:
            # Sync function from async caller: run on the threadpool so we
            # don't block the loop. contextvars propagate via to_thread.
            return await asyncio.to_thread(wrapper, *args, **kwargs)

        wrapper.sync = _sync  # type: ignore[attr-defined]
        wrapper.async_run = _async_run  # type: ignore[attr-defined]

    # `.submit(req, key=None, retry_failed=True)` — INSERT pending and return
    # a JobHandle. Worker (lazy-started on the Recorder) picks up the row.
    # Idempotency-collision: if `key` is already active, return a handle
    # bound to the existing row.
    def _submit(*args: Any, key: str | None = None, retry_failed: bool = True, **kwargs: Any) -> Any:
        from ._handle import JobHandle

        _validate_call_args(entry, key)
        recorder_inst = get_default()

        if key is not None:
            existing = _try_join_handle(recorder_inst, entry, key, retry_failed)
            if existing is not None:
                recorder_inst._ensure_worker()
                return existing

        captured_request = _capture_request(args, kwargs)
        request_json = _serialize_request(entry, captured_request)
        job_id = _storage.new_id()
        try:
            recorder_inst._insert_pending(
                job_id, entry.kind, key, _storage.now_iso(), request_json
            )
        except sqlite3.IntegrityError:
            # Another caller beat us into the active slot. Recover by
            # joining the row that exists now.
            existing = _try_join_handle(recorder_inst, entry, key, retry_failed)
            if existing is None:
                raise IdempotencyRaceError(kind=entry.kind, key=key)
            recorder_inst._ensure_worker()
            return existing

        recorder_inst._ensure_worker()
        return JobHandle(job_id, recorder_inst, entry.kind)

    wrapper.submit = _submit  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Validation, capture, serialization
# ---------------------------------------------------------------------------


def _validate_call_args(entry: _registry.RegistryEntry, key: str | None) -> None:
    """Raise if the caller is requesting idempotency without an explicit kind.

    The default kind is auto-derived from `f"{module}.{qualname}"` and
    silently changes when the function is renamed or moved — exactly the
    failure mode idempotency was meant to prevent. We refuse to compile.
    """
    if key is not None and entry.auto_kind:
        raise ConfigurationError(
            f"{entry.kind} uses an auto-derived kind (\"{entry.kind}\"). "
            "Idempotency keys require an explicit kind to remain stable across "
            "renames. Add: @recorder(kind=\"...\")"
        )


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


def _serialize_error(entry: _registry.RegistryEntry, exc: BaseException) -> str:
    """Errors are stored as `{type, message}`.

    `error=Model` is accepted in the decorator surface but not yet honored —
    the wrapper always emits the natural exception shape in phase 1.
    Custom error projection can be added without breaking the row schema.
    """
    payload = {"type": type(exc).__name__, "message": str(exc)}
    return json.dumps(payload)


def _serialize_recording_failure(exc: BaseException) -> str:
    """Error JSON for the case where the wrapped function succeeded but
    the recorder couldn't serialize its result."""
    payload = {
        "type": type(exc).__name__,
        "message": f"recorder failed to serialize response: {exc}",
    }
    return json.dumps(payload)


def _write_completion(
    recorder_inst: Recorder,
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

    Projection-from-response is best-effort: a dict response is filtered
    to the model's declared field names for dataclasses, or fed through
    `model_validate` for Pydantic. Anything else falls through with no
    projection, leaving only the attach buffer (which may be empty).
    """
    projected: dict[str, Any] = {}
    model = entry.data.model
    if model is not None and isinstance(response, dict):
        try:
            if entry.data._kind == "dataclass" and is_dataclass(model):
                import dataclasses

                names = {f.name for f in dataclasses.fields(model)}
                filtered = {k: v for k, v in response.items() if k in names}
                projected = dataclasses.asdict(model(**filtered))
            elif entry.data._kind == "pydantic":
                projected = model.model_validate(response).model_dump(mode="json")
        except Exception:
            # Projection is opportunistic — a partial response is still recorded
            # via response_json; we just skip the projected slice.
            projected = {}

    merged = {**projected, **buffer}
    if not merged:
        return None
    return json.dumps(merged)


# ---------------------------------------------------------------------------
# Idempotency join — notify primitive
# ---------------------------------------------------------------------------
#
# The wait helpers below subscribe a Future via `Recorder._subscribe()` and
# block on it. The brief calls this "wait-mechanism unification": the
# primitive used by `JobHandle.wait()` is the same primitive that the
# in-process idempotency-collision path uses.
#
# In-process: the writer of the terminal status calls `_resolve(job_id)`
# inside `_mark_completed` / `_mark_failed`, firing every subscribed
# Future. No polling occurs — `fut.result(timeout=...)` blocks the calling
# thread; `asyncio.wait_for(asyncio.wrap_future(fut), ...)` blocks the
# loop. Neither calls `time.sleep` or `asyncio.sleep`.
#
# Cross-process: the leader is in another process and never resolves our
# local Future. The wait helpers detect this by looping with a short
# `NOTIFY_POLL_INTERVAL_S` per-iteration timeout and rechecking row status
# each cycle. If status is terminal, we break and resolve. If our deadline
# passes, we raise `JoinTimeoutError`.


_NO_JOIN = object()


def _try_join_existing(
    recorder_inst: Recorder,
    entry: _registry.RegistryEntry,
    key: str,
    retry_failed: bool,
) -> Any:
    """Pre-insert collision check (sync path).

    Returns `_NO_JOIN` to mean "no existing row, proceed with INSERT".
    Otherwise returns the joined caller's response, or raises
    `JoinedSiblingFailedError` if the joined sibling terminated as failed
    (per the wrap-transparency principle: keyed calls always either return
    a response or raise; never return a `Job` for failure paths).
    """
    found = recorder_inst._lookup_active_by_kind_key(entry.kind, key)
    if found is None:
        if not retry_failed:
            failed_id = recorder_inst._lookup_latest_failed(entry.kind, key)
            if failed_id is not None:
                _raise_for_failed_sibling(recorder_inst, failed_id, entry, key)
        return _NO_JOIN

    job_id, status = found
    if status == _storage.STATUS_COMPLETED:
        return _response_for(recorder_inst, job_id)
    return _wait_for_terminal_sync(recorder_inst, job_id, entry, key)


def _wait_for_join(
    recorder_inst: Recorder,
    entry: _registry.RegistryEntry,
    key: str | None,
    retry_failed: bool,
) -> Any:
    """Reached after our INSERT lost the partial-unique-index race (sync)."""
    assert key is not None
    found = recorder_inst._lookup_active_by_kind_key(entry.kind, key)
    if found is None:
        if not retry_failed:
            failed_id = recorder_inst._lookup_latest_failed(entry.kind, key)
            if failed_id is not None:
                _raise_for_failed_sibling(recorder_inst, failed_id, entry, key)
        raise IdempotencyRaceError(kind=entry.kind, key=key)
    job_id, status = found
    if status == _storage.STATUS_COMPLETED:
        return _response_for(recorder_inst, job_id)
    return _wait_for_terminal_sync(recorder_inst, job_id, entry, key)


def _wait_for_terminal_sync(
    recorder_inst: Recorder,
    job_id: str,
    entry: _registry.RegistryEntry,
    key: str | None,
) -> Any:
    """Subscribe + block until terminal, then resolve to response or raise.

    No `time.sleep` — the Future resolves in-process the moment the
    terminal write commits. Cross-process: the loop's per-iteration
    timeout (`NOTIFY_POLL_INTERVAL_S`) doubles as the polling cadence;
    we recheck row status when the wait ticks over.
    """
    fut = recorder_inst._subscribe(job_id)
    try:
        status = _await_terminal_sync(
            recorder_inst, job_id, fut, entry, key, DEFAULT_JOIN_TIMEOUT_S
        )
    finally:
        recorder_inst._unsubscribe(job_id, fut)
    return _resolve_terminal(recorder_inst, job_id, entry, key, status)


def _await_terminal_sync(
    recorder_inst: Recorder,
    job_id: str,
    fut: concurrent.futures.Future,
    entry: _registry.RegistryEntry,
    key: str | None,
    timeout_s: float,
) -> str:
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JoinTimeoutError(
                kind=entry.kind,
                key=key,
                sibling_job_id=job_id,
                timeout_s=timeout_s,
            )
        slice_s = min(remaining, NOTIFY_POLL_INTERVAL_S)
        try:
            return fut.result(timeout=slice_s)
        except concurrent.futures.TimeoutError:
            # Cross-process fallback: another process may be the leader.
            # Recheck row status; if terminal, we missed the resolve (or
            # there was no in-process resolve to miss).
            status = recorder_inst._row_status(job_id)
            if status in _storage.TERMINAL_STATUSES:
                return status


async def _async_wait_for_terminal(
    recorder_inst: Recorder,
    job_id: str,
    entry: _registry.RegistryEntry,
    key: str | None,
) -> Any:
    """Async equivalent of `_wait_for_terminal_sync`."""
    fut = recorder_inst._subscribe(job_id)
    try:
        status = await _await_terminal_async(
            recorder_inst, job_id, fut, entry, key, DEFAULT_JOIN_TIMEOUT_S
        )
    finally:
        recorder_inst._unsubscribe(job_id, fut)
    return await asyncio.to_thread(
        _resolve_terminal, recorder_inst, job_id, entry, key, status
    )


async def _await_terminal_async(
    recorder_inst: Recorder,
    job_id: str,
    fut: concurrent.futures.Future,
    entry: _registry.RegistryEntry,
    key: str | None,
    timeout_s: float,
) -> str:
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JoinTimeoutError(
                kind=entry.kind,
                key=key,
                sibling_job_id=job_id,
                timeout_s=timeout_s,
            )
        slice_s = min(remaining, NOTIFY_POLL_INTERVAL_S)
        try:
            return await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(fut)), timeout=slice_s
            )
        except asyncio.TimeoutError:
            status = await asyncio.to_thread(
                recorder_inst._row_status, job_id
            )
            if status in _storage.TERMINAL_STATUSES:
                return status


async def _async_try_join_existing(
    recorder_inst: Recorder,
    entry: _registry.RegistryEntry,
    key: str,
    retry_failed: bool,
) -> Any:
    found = await asyncio.to_thread(
        recorder_inst._lookup_active_by_kind_key, entry.kind, key
    )
    if found is None:
        if not retry_failed:
            failed_id = await asyncio.to_thread(
                recorder_inst._lookup_latest_failed, entry.kind, key
            )
            if failed_id is not None:
                await asyncio.to_thread(
                    _raise_for_failed_sibling,
                    recorder_inst,
                    failed_id,
                    entry,
                    key,
                )
        return _NO_JOIN

    job_id, status = found
    if status == _storage.STATUS_COMPLETED:
        return await asyncio.to_thread(_response_for, recorder_inst, job_id)
    return await _async_wait_for_terminal(recorder_inst, job_id, entry, key)


async def _async_wait_for_join(
    recorder_inst: Recorder,
    entry: _registry.RegistryEntry,
    key: str | None,
    retry_failed: bool,
) -> Any:
    assert key is not None
    found = await asyncio.to_thread(
        recorder_inst._lookup_active_by_kind_key, entry.kind, key
    )
    if found is None:
        if not retry_failed:
            failed_id = await asyncio.to_thread(
                recorder_inst._lookup_latest_failed, entry.kind, key
            )
            if failed_id is not None:
                await asyncio.to_thread(
                    _raise_for_failed_sibling,
                    recorder_inst,
                    failed_id,
                    entry,
                    key,
                )
        raise IdempotencyRaceError(kind=entry.kind, key=key)
    job_id, status = found
    if status == _storage.STATUS_COMPLETED:
        return await asyncio.to_thread(_response_for, recorder_inst, job_id)
    return await _async_wait_for_terminal(recorder_inst, job_id, entry, key)


# --- shared resolution helpers ---


def _response_for(recorder_inst: Recorder, job_id: str) -> Any:
    job = recorder_inst.get(job_id)
    return job.response if job is not None else None


def _resolve_terminal(
    recorder_inst: Recorder,
    job_id: str,
    entry: _registry.RegistryEntry,
    key: str | None,
    status: str,
) -> Any:
    if status == _storage.STATUS_COMPLETED:
        return _response_for(recorder_inst, job_id)
    _raise_for_failed_sibling(recorder_inst, job_id, entry, key)


def _try_join_handle(
    recorder_inst: Recorder,
    entry: _registry.RegistryEntry,
    key: str | None,
    retry_failed: bool,
) -> Any:
    """For `.submit(key=...)`: return a `JobHandle` over the existing
    active row, or `None` if no active row exists and the caller should
    INSERT fresh.

    Mirrors `_try_join_existing` shape but returns a `JobHandle` instead
    of awaiting terminal — the caller wants a handle, not a response.
    Failed-row + `retry_failed=False`: returns a handle whose `.wait()`
    will raise `JoinedSiblingFailedError` (per wrap-transparency).
    """
    from ._handle import JobHandle

    if key is None:
        return None
    found = recorder_inst._lookup_active_by_kind_key(entry.kind, key)
    if found is not None:
        job_id, _status = found
        return JobHandle(job_id, recorder_inst, entry.kind)
    if not retry_failed:
        failed_id = recorder_inst._lookup_latest_failed(entry.kind, key)
        if failed_id is not None:
            return JobHandle(failed_id, recorder_inst, entry.kind)
    return None


def _raise_for_failed_sibling(
    recorder_inst: Recorder,
    job_id: str,
    entry: _registry.RegistryEntry,
    key: str | None,
) -> None:
    job = recorder_inst.get(job_id)
    sibling_error = (
        job.error if (job is not None and isinstance(job.error, dict)) else None
    )
    raise JoinedSiblingFailedError(
        kind=entry.kind,
        key=key or "",
        sibling_job_id=job_id,
        sibling_error=sibling_error,
    )
