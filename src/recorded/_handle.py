"""`JobHandle`: returned from `wrapper.submit()`.

Both `.wait()` (async) and `.wait_sync()` (sync) subscribe to the wait
primitive on first call and resolve to a `Job` on success or raise
`JoinedSiblingFailedError` on failure. Wrap-transparency: the success
return type is always `Job`; failure is always an exception.

Also exposes module-level `_wait_for_terminal_async` /
`_wait_for_terminal_sync` helpers so the bare-call idempotency-join
path (in `_decorator`) can reuse the same polling loop without
re-implementing the cross-process fallback or risking the wrap_future
accumulation bug in two places.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from typing import TYPE_CHECKING

from . import _storage
from ._errors import (
    JoinedSiblingFailedError,
    JoinTimeoutError,
    RowDisappearedError,
    SyncInLoopError,
)
from ._recorder import NOTIFY_POLL_INTERVAL_S
from ._types import Job

if TYPE_CHECKING:
    from ._recorder import Recorder


# ---------------------------------------------------------------------------
# Shared wait-for-terminal helpers
# ---------------------------------------------------------------------------
#
# Used by both `JobHandle.wait()` / `wait_sync()` and the bare-call
# idempotency-join path in `_decorator`. One implementation, one place
# to fix the cross-process polling cadence or the wrap_future hoisting.


def _wait_for_terminal_sync(
    recorder: Recorder,
    fut: concurrent.futures.Future,
    job_id: str,
    kind: str,
    key: str | None,
    timeout_s: float,
) -> str:
    """Block until `fut` resolves with a terminal status, with cross-
    process polling fallback. Returns the terminal status string.

    Raises `JoinTimeoutError` if `timeout_s` elapses first, or
    `RowDisappearedError` if the row is deleted while we're parked.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JoinTimeoutError(
                kind=kind, key=key, sibling_job_id=job_id, timeout_s=timeout_s,
            )
        slice_s = min(remaining, NOTIFY_POLL_INTERVAL_S)
        try:
            return fut.result(timeout=slice_s)
        except concurrent.futures.TimeoutError:
            # Cross-process fallback: another process may be the leader,
            # so our local Future never resolves. Recheck row status.
            status = recorder._row_status(job_id)
            if status in _storage.TERMINAL_STATUSES:
                return status
            if status is None:
                raise RowDisappearedError(
                    kind=kind, key=key, sibling_job_id=job_id,
                ) from None


async def _wait_for_terminal_async(
    recorder: Recorder,
    fut: concurrent.futures.Future,
    job_id: str,
    kind: str,
    key: str | None,
    timeout_s: float,
) -> str:
    """Async variant of `_wait_for_terminal_sync`. SQL status checks go
    through `asyncio.to_thread` so they don't block the loop.

    `wrap_future` is called once before the loop — each invocation
    registers a done-callback on the underlying concurrent.futures.Future,
    so creating it inside the loop body would accumulate callbacks on
    every cross-process polling tick.
    """
    async_fut = asyncio.wrap_future(fut)
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JoinTimeoutError(
                kind=kind, key=key, sibling_job_id=job_id, timeout_s=timeout_s,
            )
        slice_s = min(remaining, NOTIFY_POLL_INTERVAL_S)
        try:
            return await asyncio.wait_for(
                asyncio.shield(async_fut), timeout=slice_s,
            )
        except asyncio.TimeoutError:
            status = await asyncio.to_thread(recorder._row_status, job_id)
            if status in _storage.TERMINAL_STATUSES:
                return status
            if status is None:
                raise RowDisappearedError(
                    kind=kind, key=key, sibling_job_id=job_id,
                ) from None


class JobHandle:
    """Reference to a submitted job, with sync + async wait surfaces."""

    __slots__ = ("job_id", "_recorder", "_kind")

    def __init__(self, job_id: str, recorder: Recorder, kind: str) -> None:
        self.job_id = job_id
        self._recorder = recorder
        self._kind = kind

    def __repr__(self) -> str:  # for test failures and debugging
        return f"JobHandle(job_id={self.job_id!r}, kind={self._kind!r})"

    # ----- async wait -----

    async def wait(self, timeout: float | None = None) -> Job:
        """Async wait. Returns the terminal `Job` on success.

        Raises `JoinedSiblingFailedError` if the job terminated as failed
        (per the wrap-transparency principle: keyed-call surfaces never
        return a `Job` for failure paths). Raises `JoinTimeoutError`
        (also a stdlib `TimeoutError`) if `timeout` elapses first.
        """
        timeout_s = (
            self._recorder.join_timeout_s if timeout is None else float(timeout)
        )
        fut = self._recorder._subscribe(self.job_id)
        try:
            status = await _wait_for_terminal_async(
                self._recorder, fut, self.job_id, self._kind, None, timeout_s,
            )
        finally:
            self._recorder._unsubscribe(self.job_id, fut)
        try:
            return self._resolve_to_job(status)
        finally:
            # Drain the live-result cache. JobHandle.wait() returns the
            # storage-rehydrated `Job`, so the cache entry would otherwise
            # leak on the no-sibling-joiner path. A racing same-key bare-
            # call sibling that consumes first is a no-op for us.
            self._recorder._take_live_result(self.job_id)

    # ----- sync wait -----

    def wait_sync(self, timeout: float | None = None) -> Job:
        """Sync wait. Returns the terminal `Job` on success.

        Raises `SyncInLoopError` if called from inside a running event
        loop (matches `.sync()` precedent). Otherwise blocks the calling
        thread until the job reaches terminal status.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SyncInLoopError(
                "JobHandle.wait_sync() was called from inside a running "
                "event loop. Use `await handle.wait()` instead."
            )

        timeout_s = (
            self._recorder.join_timeout_s if timeout is None else float(timeout)
        )
        fut = self._recorder._subscribe(self.job_id)
        try:
            status = _wait_for_terminal_sync(
                self._recorder, fut, self.job_id, self._kind, None, timeout_s,
            )
        finally:
            self._recorder._unsubscribe(self.job_id, fut)
        try:
            return self._resolve_to_job(status)
        finally:
            # See `wait()` above: drain the live-result cache.
            self._recorder._take_live_result(self.job_id)

    # ----- helpers -----

    def _resolve_to_job(self, status: str) -> Job:
        job = self._recorder.get(self.job_id)
        if job is None:
            # Vanishingly rare: row deleted (or DROP TABLE) between
            # `_resolve` firing and our re-fetch.
            raise RuntimeError(
                f"JobHandle({self.job_id!r}) resolved to status {status!r} "
                "but the row could not be re-fetched."
            )
        if status == _storage.STATUS_FAILED:
            # Raw dict (not the rehydrated model) so callers programmatically
            # branching on `sibling_error` see the same shape regardless of
            # whether the decorator was registered with `error=Model`.
            sibling_error = self._recorder._row_error_dict(self.job_id)
            raise JoinedSiblingFailedError(
                kind=self._kind,
                key=job.key or "",
                sibling_job_id=self.job_id,
                sibling_error=sibling_error,
            )
        return job
