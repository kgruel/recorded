"""CLI subcommand handlers for `python -m recorded`.

Stdlib only. Each handler builds its own short-lived `Recorder(path=...)`
and shuts it down in `finally`. The CLI never touches the module-level
default singleton — a CLI invocation is a separate process whose
Recorder lifetime ends with the command.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from typing import Any, TextIO

from . import _storage
from ._recorder import Recorder
from ._types import Job


# One-line row format used by `last` and `tail`.
def _format_row(job: Job) -> str:
    return f"{job.submitted_at} {job.status:9} {job.kind:30} {job.id[:8]} key={job.key or '-'}"


def cmd_last(args, *, stdout: TextIO = sys.stdout) -> int:
    r = Recorder(path=args.path)
    try:
        jobs = r.last(args.n, kind=args.kind, status=args.status)
        for job in jobs:
            print(_format_row(job), file=stdout)
        return 0
    finally:
        r.shutdown()


def cmd_get(
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


def cmd_tail(
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
                    r.list(
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


# argparse builder lives here so __main__.py is one-line dispatch.
def build_parser() -> Any:
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
