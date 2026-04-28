"""CLI subcommand handlers for `python -m recorded`.

Stdlib only. Each handler builds its own short-lived `Recorder(path=...)`
and shuts it down in `finally`. The CLI never touches the module-level
default singleton — a CLI invocation is a separate process whose
Recorder lifetime ends with the command.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import os
import signal
import socket
import sys
import time
import warnings
from typing import Any, TextIO

from . import _registry, _storage
from ._errors import RecordedWarning
from ._lifecycle import _run_and_record_async, make_error_json
from ._recorder import Recorder
from ._types import Job

_logger = logging.getLogger("recorded")


# One-line row format used by `last` and `tail`.
def _format_row(job: Job) -> str:
    return f"{job.submitted_at} {job.status:9} {job.kind:30} {job.id[:8]} key={job.key or '-'}"


def cmd_last(args, *, stdout: TextIO = sys.stdout) -> int:  # pragma: no cover
    r = Recorder(path=args.path)
    try:
        jobs = r.last(args.n, kind=args.kind, status=args.status)
        for job in jobs:
            print(_format_row(job), file=stdout)
        return 0
    finally:
        r.shutdown()


def cmd_get(  # pragma: no cover
    args,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    r = Recorder(path=args.path)
    try:
        job = r.get(args.job_id)
        if job is None:
            print(f"error: no job with id {args.job_id}", file=stderr)
            return 2
        if args.prompt:
            print(job.to_prompt(), file=stdout)
        else:
            print(
                json.dumps(
                    dataclasses.asdict(job),
                    indent=2,
                    sort_keys=False,
                    default=str,
                ),
                file=stdout,
            )
        return 0
    finally:
        r.shutdown()


def cmd_tail(  # pragma: no cover
    args,
    *,
    stdout: TextIO = sys.stdout,
) -> int:
    r = Recorder(path=args.path)
    try:
        # Initial watermark: now. No historical replay — `last` covers history.
        watermark = _storage.now_iso()
        seen_ids: set[str] = set()
        try:
            while True:
                # Single query covers both terminal statuses; since= uses `>=`
                # so we de-dup via seen_ids.
                rows: list[Job] = list(
                    r.query(
                        kind=args.kind,
                        status=("completed", "failed"),
                        since=watermark,
                        limit=1000,
                        order="asc",
                    )
                )
                new_max = watermark
                for job in rows:
                    if job.id in seen_ids:
                        continue
                    print(_format_row(job), file=stdout, flush=True)
                    seen_ids.add(job.id)
                    if job.submitted_at > new_max:
                        new_max = job.submitted_at
                # Advance watermark; trim seen_ids to those at the boundary
                # so the set doesn't grow unboundedly.
                if new_max != watermark:
                    watermark = new_max
                    seen_ids = {j.id for j in rows if j.submitted_at == new_max}
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    finally:
        r.shutdown()


# ---------------------------------------------------------------------------
# `run` — leader process: claim heartbeat slot, claim+execute pending rows
# ---------------------------------------------------------------------------


def cmd_run(  # pragma: no cover — subprocess-tested in test_cli_run.py
    args,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run as the leader process for the given `jobs.db`.

    The leader:
      1. Claims a `_recorded.leader` heartbeat row keyed by `host:pid`.
      2. Spawns a heartbeat task that refreshes `started_at` every
         `leader_heartbeat_s` seconds.
      3. Loops on `Recorder._claim_one()`; for each claimed row, spawns an
         asyncio task that runs the wrapped function via
         `_run_and_record_async`.
      4. On SIGTERM/SIGINT: stops claiming new rows, waits for in-flight
         tasks to drain (bounded by `args.shutdown_timeout`), then
         DELETEs the heartbeat row and exits 0.

    Stdlib-only. One `asyncio.run(...)` for the whole leader lifetime.
    """
    rec = Recorder(path=args.path)
    try:
        return asyncio.run(_lead_forever(rec, args, stdout=stdout, stderr=stderr))
    finally:
        rec.shutdown()


async def _lead_forever(  # pragma: no cover — subprocess-tested
    rec: Recorder,
    args,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    host_pid = f"{socket.gethostname()}:{os.getpid()}"
    leader_id = await asyncio.to_thread(rec._claim_leader_slot, host_pid)
    print(
        f"recorded: leader started ({host_pid}, leader_id={leader_id[:8]})",
        file=stdout,
        flush=True,
    )

    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows (and some sandboxed environments) doesn't support
            # signal.add_signal_handler. Fall back to KeyboardInterrupt /
            # process death; the heartbeat row will eventually be reaped.
            pass

    heartbeat_task = asyncio.create_task(_heartbeat_loop(rec, leader_id, shutdown_event))
    in_flight: set[asyncio.Task] = set()

    try:
        while not shutdown_event.is_set():
            row = await asyncio.to_thread(rec._claim_one)
            if shutdown_event.is_set():
                if row is not None:
                    # Race: claimed a row but shutdown fired before we could
                    # spawn its task. Mark failed so the row isn't stuck
                    # `running` waiting for the reaper threshold.
                    await asyncio.to_thread(
                        rec._mark_failed,
                        row[0],
                        _storage.now_iso(),
                        make_error_json(
                            "CancelledError",
                            "leader shutdown cancelled in-flight job",
                        ),
                    )
                break
            if row is None:
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=rec.worker_poll_interval_s,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(_execute_claimed_row(rec, row))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
    finally:
        # Drain in-flight tasks within the configured shutdown timeout.
        if in_flight:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*in_flight, return_exceptions=True),
                    timeout=args.shutdown_timeout,
                )
            except asyncio.TimeoutError:
                # Some tasks didn't drain — emit dual-channel hazard
                # (logger + warnings.warn) per `docs/HOW.md::Warnings policy`,
                # then cancel them and let their finally blocks write whatever
                # they can.
                stragglers = len([t for t in in_flight if not t.done()])
                msg = (
                    f"recorded: leader did not drain {stragglers} in-flight "
                    f"task(s) within {args.shutdown_timeout:.1f}s — cancelling. "
                    "In-flight rows recorded as CancelledError."
                )
                _logger.warning("%s", msg)
                warnings.warn(msg, RecordedWarning, stacklevel=2)
                for t in in_flight:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*in_flight, return_exceptions=True)

        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        # Graceful release: DELETE our running heartbeat row so it doesn't
        # linger in the audit log until the reaper sweeps it. Keyed on
        # host_pid (not leader_id) because `_heartbeat_loop` may have
        # rebound leader_id after a resurrection — host_pid is stable.
        try:
            await asyncio.to_thread(rec._release_leader_slot, host_pid)
        except Exception:
            pass
        print(f"recorded: leader stopped ({host_pid})", file=stdout, flush=True)
    return 0


async def _heartbeat_loop(
    rec: Recorder,
    leader_id: str,
    shutdown_event: asyncio.Event,
) -> None:
    """Refresh the leader heartbeat row every `rec.leader_heartbeat_s`.

    If `_touch_leader_heartbeat` returns False, our row was reaped (we
    were unresponsive past `reaper_threshold_s`). Re-claim a fresh slot
    so `is_leader_running()` keeps returning True for `.submit()` callers.
    """
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=rec.leader_heartbeat_s,
            )
            return  # shutdown fired during the sleep
        except asyncio.TimeoutError:
            pass
        try:
            ok = await asyncio.to_thread(rec._touch_leader_heartbeat, leader_id)
        except Exception:
            _logger.exception("recorded: heartbeat refresh failed")
            continue
        if not ok:
            # Resurrection edge: our heartbeat row was reaped (we were
            # unresponsive past `reaper_threshold_s`). Re-claim a slot so
            # `is_leader_running()` keeps returning True. Lifecycle hazard:
            # tasks that ran *while* we were unresponsive may have written
            # spurious-reaper-marked rows — surface dual-channel
            # (logger + warnings.warn) per `docs/HOW.md::Warnings policy`.
            try:
                host_pid = f"{socket.gethostname()}:{os.getpid()}"
                leader_id = await asyncio.to_thread(rec._claim_leader_slot, host_pid)
                msg = (
                    f"recorded: leader heartbeat was reaped — re-claimed slot "
                    f"(host_pid={host_pid}, new leader_id={leader_id[:8]}). "
                    "Some in-flight rows may have been marked as orphaned; "
                    "investigate process responsiveness or raise "
                    "reaper_threshold_s."
                )
                _logger.warning("%s", msg)
                warnings.warn(msg, RecordedWarning, stacklevel=2)
            except Exception:
                _logger.exception("recorded: failed to re-claim leader slot after reap")


async def _execute_claimed_row(rec: Recorder, row: tuple) -> None:
    """Run the wrapped function for one claimed row.

    Delegates to `_run_and_record_async` for the universal serialize-
    validate-record flow, with UnknownKind handling for kinds whose
    decorator wasn't imported in the leader process (the operator must
    pass `--import package.module` so `@recorder` decorations register).
    """
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
        err = make_error_json(
            "UnknownKind",
            f"No registered function for kind {kind!r}. "
            "Ensure the module defining this kind is imported "
            "in the leader process (e.g. via `--import package.module`).",
        )
        await asyncio.to_thread(rec._mark_failed, job_id, _storage.now_iso(), err)
        return

    request = entry.request.deserialize(json.loads(request_json) if request_json else None)
    fn = entry.fn

    if inspect.iscoroutinefunction(fn):

        async def _invoke():
            return await fn(request)
    else:

        async def _invoke():
            return await asyncio.to_thread(fn, request)

    try:
        await _run_and_record_async(rec, entry, job_id, key, _invoke)
    except asyncio.CancelledError:
        try:
            rec._mark_failed(
                job_id,
                _storage.now_iso(),
                make_error_json(
                    "CancelledError",
                    "leader shutdown cancelled in-flight job",
                ),
            )
        except Exception:
            pass
        raise
    except Exception:
        # _run_and_record_async already recorded failure; nothing to do.
        return


# argparse builder lives here so __main__.py is one-line dispatch.
def build_parser() -> Any:  # pragma: no cover — exercised via subprocess CLI tests
    import argparse

    p = argparse.ArgumentParser(prog="python -m recorded")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_path(sp):
        sp.add_argument(
            "--path",
            default="./jobs.db",
            help="SQLite DB path (default: ./jobs.db)",
        )

    p_last = sub.add_parser("last", help="Print most recent N jobs.")
    p_last.add_argument("n", nargs="?", type=int, default=10)
    p_last.add_argument("--kind", default=None, help="Glob filter on kind.")
    p_last.add_argument("--status", default=None, help="Exact status filter.")
    _add_path(p_last)
    p_last.set_defaults(func=cmd_last)

    p_get = sub.add_parser("get", help="Print one job by id.")
    p_get.add_argument("job_id")
    p_get.add_argument(
        "--prompt",
        action="store_true",
        help="Emit Job.to_prompt() (Markdown) instead of JSON.",
    )
    _add_path(p_get)
    p_get.set_defaults(func=cmd_get)

    p_tail = sub.add_parser("tail", help="Stream new terminal rows since a moving watermark.")
    p_tail.add_argument("--kind", default=None, help="Glob filter on kind.")
    p_tail.add_argument("--interval", type=float, default=1.0, help="Poll interval seconds.")
    _add_path(p_tail)
    p_tail.set_defaults(func=cmd_tail)

    p_run = sub.add_parser(
        "run",
        help="Run as the leader process: claim and execute pending .submit() rows.",
    )
    _add_path(p_run)
    p_run.add_argument(
        "--import",
        dest="imports",
        action="append",
        default=[],
        metavar="MODULE",
        help=(
            "Import this module before claiming rows (so `@recorder` runs and "
            "registers kinds). Repeat for multiple modules."
        ),
    )
    p_run.add_argument(
        "--shutdown-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for in-flight tasks on SIGTERM/SIGINT (default: 10).",
    )
    p_run.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = build_parser()
    args = parser.parse_args(argv)
    # `run` may need user modules imported so their `@recorder` decorations
    # register kinds in this process before we start claiming rows.
    for mod in getattr(args, "imports", None) or []:
        __import__(mod)
    return args.func(args)
