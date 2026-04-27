"""Stuck-row reaper: runs on Recorder construction.

Any `running` row whose `started_at` predates `reaper_threshold_s` is
flipped to `failed` with an `{type: "orphaned", reason: ...}` error
payload. Idempotent across processes (conditional UPDATE).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from recorded import Recorder
from recorded import _storage


def _seed_running_row(
    db_path: str,
    *,
    job_id: str,
    started_at: str,
    submitted_at: str | None = None,
) -> None:
    """Open a short-lived Recorder (with reaper disabled) to insert a row,
    then close it. Construction with `reaper_threshold_s=` doesn't help
    here because we want the row to *predate* the reaper running; the
    next Recorder we open has the default short threshold and reaps it."""
    conn = _storage.open_connection(db_path)
    try:
        _storage.ensure_schema(conn)
        conn.execute(
            _storage.INSERT_PENDING,
            (job_id, "t.reap", None, submitted_at or started_at, None),
        )
        conn.execute(_storage.UPDATE_RUNNING, (started_at, job_id))
    finally:
        conn.close()


def test_reaper_marks_orphaned_running_rows_failed_on_start(db_path):
    """Named test: pre-seed a `running` row with stale `started_at`; the
    next `Recorder(...)` reaps it on construction."""
    stale_started = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    job_id = _storage.new_id()
    _seed_running_row(db_path, job_id=job_id, started_at=stale_started)

    # Default reaper threshold is 5 min; this row's started_at is 10 min stale.
    rec = Recorder(path=db_path)
    try:
        # Construction triggers the reaper via `_connection()`'s lazy init.
        rec.connection()
        job = rec.get(job_id)
        assert job is not None
        assert job.status == _storage.STATUS_FAILED
        assert isinstance(job.error, dict)
        assert job.error == {
            "type": "orphaned",
            "reason": "process_died_while_running",
        }
        assert job.completed_at is not None
    finally:
        rec.shutdown()


def test_reaper_leaves_recent_running_rows_alone(db_path):
    """A row whose `started_at` is within the threshold is NOT reaped."""
    fresh_started = _storage.now_iso()
    job_id = _storage.new_id()
    _seed_running_row(db_path, job_id=job_id, started_at=fresh_started)

    rec = Recorder(path=db_path, reaper_threshold_s=300.0)
    try:
        rec.connection()
        job = rec.get(job_id)
        assert job is not None
        assert job.status == _storage.STATUS_RUNNING
    finally:
        rec.shutdown()


def test_reaper_threshold_configurable_via_keyword(db_path):
    """`Recorder(reaper_threshold_s=0)` reaps every `running` row."""
    fresh_started = _storage.now_iso()
    job_id = _storage.new_id()
    _seed_running_row(db_path, job_id=job_id, started_at=fresh_started)

    rec = Recorder(path=db_path, reaper_threshold_s=0.0)
    try:
        rec.connection()
        job = rec.get(job_id)
        assert job is not None
        assert job.status == _storage.STATUS_FAILED
    finally:
        rec.shutdown()


def test_reaper_late_completion_silently_dropped(db_path):
    """Named test: start a job, reap it, then attempt completion. The
    row stays `failed` with the reaper's `error_json`; the late
    `_mark_completed` no-ops because the conditional UPDATE doesn't match."""
    stale_started = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    job_id = _storage.new_id()
    _seed_running_row(db_path, job_id=job_id, started_at=stale_started)

    rec = Recorder(path=db_path)
    try:
        rec.connection()  # triggers reap
        job = rec.get(job_id)
        assert job is not None
        assert job.status == _storage.STATUS_FAILED

        # Late completion: status is no longer 'running', UPDATE matches 0 rows.
        rec._mark_completed(
            job_id, _storage.now_iso(), '{"late": true}', None
        )
        job_after = rec.get(job_id)
        assert job_after is not None
        assert job_after.status == _storage.STATUS_FAILED
        # Reaper's error mark survives.
        assert job_after.error == {
            "type": "orphaned",
            "reason": "process_died_while_running",
        }
        # Response stayed null — the late UPDATE was a no-op.
        assert job_after.response is None
    finally:
        rec.shutdown()


def test_reaper_resolves_subscribers_for_reaped_jobs(db_path):
    """A subscriber waiting on a row that gets reaped during construction
    is resolved with `failed`. (Edge case: subscribe before the reaper
    flips the row.)

    Mainly a defense against accidental regressions: the reaper SHOULD
    fire `_resolve` on every reaped id, so cross-process joiners that
    are also in-process don't hang past the reap.
    """
    stale_started = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    job_id = _storage.new_id()
    _seed_running_row(db_path, job_id=job_id, started_at=stale_started)

    rec = Recorder(path=db_path)
    try:
        # Subscribe AFTER reap (since reaper runs in connection bootstrap
        # and we don't have a hook to register before it fires). The row
        # is already terminal, so `_subscribe` pre-resolves.
        rec.connection()
        fut = rec._subscribe(job_id)
        assert fut.done()
        assert fut.result(timeout=0) == _storage.STATUS_FAILED
    finally:
        rec.shutdown()
