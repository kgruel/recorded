"""Worker: a single asyncio loop in a dedicated thread.

Lazy-started on the first `.submit()` against any wrapped function on a
given Recorder. Bare calls and `.sync()` / `.async_run()` shims do NOT
spin up a worker — a script that only ever uses bare calls runs without
any background thread.

Concurrency model: one event loop, many in-flight tasks. The loop polls
for `pending` rows via `Recorder._claim_one()` (atomic UPDATE…RETURNING),
spawns each as a task, and continues. SQLite I/O is dispatched via
`asyncio.to_thread`.

Shutdown: idempotent. Sets a loop-side `asyncio.Event`, cancels any
in-flight tasks, and joins the worker thread. In-flight rows are marked
`failed` synchronously inside each task's `finally` so the write
completes before the cancellation propagates further.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from typing import TYPE_CHECKING

from . import _registry, _storage
from ._lifecycle import make_error_json

if TYPE_CHECKING:
    from ._recorder import Recorder

_logger = logging.getLogger("recorded")


# Error payload for tasks aborted by `Recorder.shutdown()`.
_CANCEL_ERROR_JSON = make_error_json(
    "CancelledError", "worker shutdown cancelled in-flight job"
)


class Worker:
    """Owns one asyncio event loop running on a dedicated thread."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # asyncio.Event in 3.10+ binds to a loop on first use, not at
        # construction — safe to create here on the main thread; the
        # binding lands on the worker loop the first time `_main()`
        # touches it.
        self._loop_shutdown = asyncio.Event()
        self._loop_ready = threading.Event()
        self._started = False
        self._shutdown_called = False
        self._lock = threading.Lock()

    # ----- start -----

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._run, name="recorded-worker", daemon=True
            )
            self._thread.start()
        # Block until the loop is created so a `submit()` racing the
        # start has a coherent target.
        self._loop_ready.wait()

    # ----- thread entrypoint -----

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_until_complete(self._main())
        finally:
            try:
                # Drain any tasks created (cancellations) during _main exit.
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                loop.close()

    # ----- main loop -----

    async def _main(self) -> None:
        in_flight: set[asyncio.Task] = set()
        try:
            while not self._loop_shutdown.is_set():
                row = await asyncio.to_thread(self.recorder._claim_one)
                if self._loop_shutdown.is_set():
                    # Race window: claimed a row but shutdown fired before
                    # we could spawn its task. Mark it failed so the row
                    # isn't stuck `running` for the reaper to pick up.
                    if row is not None:
                        await asyncio.to_thread(
                            self.recorder._mark_failed,
                            row[0],
                            _storage.now_iso(),
                            _CANCEL_ERROR_JSON,
                        )
                    break
                if row is None:
                    # No work; wait for the poll interval, returning early
                    # if shutdown fires.
                    try:
                        await asyncio.wait_for(
                            self._loop_shutdown.wait(),
                            timeout=self.recorder.worker_poll_interval_s,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                task = asyncio.create_task(self._execute(row))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            # Drain on shutdown: cancel in-flight tasks and let their
            # finally blocks write the cancellation marker.
            for t in list(in_flight):
                t.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    # ----- per-job execution -----

    async def _execute(self, row: tuple) -> None:
        """Run the wrapped function for one claimed row.

        Delegates context-set + invoke + record-outcome to
        `_run_and_record_async`. The worker's only added job is to map
        cancellation (worker-shutdown) onto the cancel marker, and to
        swallow recorded exceptions (the bare-call path re-raises them
        to the user; the worker has no caller, so they're done once
        recorded).
        """
        from ._lifecycle import _run_and_record_async

        (
            job_id,
            kind,
            key,
            _status,
            _submitted_at,
            _started_at,
            _completed_at,
            request_json,
            _response_json,
            _data_json,
            _error_json,
        ) = row

        entry = _registry.lookup(kind)
        if entry is None or entry.fn is None:
            # No registered function — happens if a worker process didn't
            # import the module that defined this kind. Mark failed and
            # continue.
            err = make_error_json(
                "UnknownKind",
                f"No registered function for kind {kind!r}. "
                "Ensure the module defining this kind is imported "
                "in the worker process.",
            )
            await asyncio.to_thread(
                self.recorder._mark_failed, job_id, _storage.now_iso(), err
            )
            return

        request = entry.request.deserialize(_loads(request_json))

        fn = entry.fn

        if inspect.iscoroutinefunction(fn):
            async def _invoke():
                return await fn(request)
        else:
            async def _invoke():
                return await asyncio.to_thread(fn, request)

        try:
            await _run_and_record_async(
                self.recorder, entry, job_id, key, _invoke
            )
        except asyncio.CancelledError:
            # Worker shutdown cancelled this task. The recording layer
            # didn't write a terminal row (CancelledError bypasses its
            # except-Exception). Synchronous mark so the write commits
            # before CancelledError unwinds further.
            try:
                self.recorder._mark_failed(
                    job_id, _storage.now_iso(), _CANCEL_ERROR_JSON
                )
            except Exception:
                pass
            raise
        except Exception:
            # Already recorded as failed by _run_and_record_async (either
            # the wrapped fn raised, or recording itself failed). The
            # worker has no caller to propagate to — swallow.
            return

    # ----- shutdown -----

    def shutdown(self, *, timeout: float = 10.0) -> None:
        """Idempotent: signal shutdown, cancel in-flight, join the thread.

        If the worker thread does not drain within `timeout`, emits a warning
        rather than returning silently — the caller cannot otherwise tell
        "drained cleanly" from "join timed out and the daemon thread is still
        running and about to be killed at interpreter teardown."
        """
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            if not self._started:
                return

        # Signal the loop to stop. `loop.call_soon_threadsafe` schedules
        # the event-set on the loop's thread so the existing
        # `wait_for(self._loop_shutdown.wait(), ...)` resolves.
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._loop_shutdown.set)

        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                _logger.warning(
                    "recorded-worker did not drain within %.3fs. In-flight "
                    "tasks remain on the daemon thread and may be killed at "
                    "interpreter teardown — successful results in flight are "
                    "lost (recorded as CancelledError if anything is recorded "
                    "at all). Increase Worker.shutdown(timeout=) or shorten "
                    "the wrapped function.",
                    timeout,
                )


# ----- helpers ----------------------------------------------------------------


def _loads(s: str | None):
    return None if s is None else json.loads(s)
