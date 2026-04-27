"""`JobHandle`: returned from `wrapper.submit()`.

Both `.wait()` (async) and `.wait_sync()` (sync) subscribe to the wait
primitive on first call and resolve to a `Job` on success or raise
`JoinedSiblingFailedError` on failure. Wrap-transparency: the success
return type is always `Job`; failure is always an exception.
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
    SyncInLoopError,
)
from ._recorder import DEFAULT_JOIN_TIMEOUT_S, NOTIFY_POLL_INTERVAL_S
from ._types import Job

if TYPE_CHECKING:
    from ._recorder import Recorder


class JobHandle:
    """Reference to a submitted job, with sync + async wait surfaces."""

    __slots__ = ("job_id", "_recorder", "_kind")

    def __init__(self, job_id: str, recorder: "Recorder", kind: str) -> None:
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
        timeout_s = DEFAULT_JOIN_TIMEOUT_S if timeout is None else float(timeout)
        fut = self._recorder._subscribe(self.job_id)
        try:
            status = await self._await_async(fut, timeout_s)
        finally:
            self._recorder._unsubscribe(self.job_id, fut)
        return self._resolve_to_job(status)

    async def _await_async(
        self, fut: concurrent.futures.Future, timeout_s: float
    ) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JoinTimeoutError(
                    kind=self._kind,
                    key=None,
                    sibling_job_id=self.job_id,
                    timeout_s=timeout_s,
                )
            slice_s = min(remaining, NOTIFY_POLL_INTERVAL_S)
            try:
                return await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(fut)),
                    timeout=slice_s,
                )
            except asyncio.TimeoutError:
                # Cross-process fallback — recheck row status in case the
                # leader is in another process.
                status = await asyncio.to_thread(
                    self._recorder._row_status, self.job_id
                )
                if status in _storage.TERMINAL_STATUSES:
                    return status

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

        timeout_s = DEFAULT_JOIN_TIMEOUT_S if timeout is None else float(timeout)
        fut = self._recorder._subscribe(self.job_id)
        try:
            status = self._await_sync(fut, timeout_s)
        finally:
            self._recorder._unsubscribe(self.job_id, fut)
        return self._resolve_to_job(status)

    def _await_sync(
        self, fut: concurrent.futures.Future, timeout_s: float
    ) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JoinTimeoutError(
                    kind=self._kind,
                    key=None,
                    sibling_job_id=self.job_id,
                    timeout_s=timeout_s,
                )
            slice_s = min(remaining, NOTIFY_POLL_INTERVAL_S)
            try:
                return fut.result(timeout=slice_s)
            except concurrent.futures.TimeoutError:
                status = self._recorder._row_status(self.job_id)
                if status in _storage.TERMINAL_STATUSES:
                    return status

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
            sibling_error = (
                job.error if isinstance(job.error, dict) else None
            )
            raise JoinedSiblingFailedError(
                kind=self._kind,
                key=job.key or "",
                sibling_job_id=self.job_id,
                sibling_error=sibling_error,
            )
        return job
