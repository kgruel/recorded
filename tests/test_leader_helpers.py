"""Unit tests for the in-process leader helpers in `_cli.py`.

`test_cli_run.py` already tests the leader as a subprocess (which is
the production shape). The subprocess tests don't show up in pytest-cov
output because coverage doesn't follow Popen children, so these tests
exercise the same helpers in-process to keep the coverage signal
honest. They're tightly scoped — no full `cmd_run` invocation, just
the per-row execution helper and the heartbeat loop.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from recorded import Recorder, _storage, recorder
from recorded._cli import _execute_claimed_row, _heartbeat_loop


@pytest.mark.asyncio
async def test_execute_claimed_row_runs_sync_function(default_recorder):
    @recorder(kind="t.leader.helper.sync")
    def fn(req):
        return {"echoed": req}

    rec: Recorder = default_recorder
    job_id = _storage.new_id()
    now = _storage.now_iso()
    rec._insert_pending(job_id, "t.leader.helper.sync", None, now, json.dumps("hello"))
    # Move the row to running so _execute_claimed_row sees the same shape
    # claim_one would have produced.
    rec._execute(_storage.UPDATE_RUNNING, (now, job_id))
    row = rec._fetchone(
        f"SELECT {', '.join(_storage.COLUMNS)} FROM jobs WHERE id=?",
        (job_id,),
    )
    assert row is not None

    await _execute_claimed_row(rec, row)

    job = rec.get(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.response == {"echoed": "hello"}


@pytest.mark.asyncio
async def test_execute_claimed_row_runs_async_function(default_recorder):
    @recorder(kind="t.leader.helper.async")
    async def fn(req):
        await asyncio.sleep(0.001)
        return {"echoed_async": req}

    rec: Recorder = default_recorder
    job_id = _storage.new_id()
    now = _storage.now_iso()
    rec._insert_pending(job_id, "t.leader.helper.async", None, now, json.dumps(123))
    rec._execute(_storage.UPDATE_RUNNING, (now, job_id))
    row = rec._fetchone(
        f"SELECT {', '.join(_storage.COLUMNS)} FROM jobs WHERE id=?",
        (job_id,),
    )

    await _execute_claimed_row(rec, row)

    job = rec.get(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.response == {"echoed_async": 123}


@pytest.mark.asyncio
async def test_execute_claimed_row_records_failure(default_recorder):
    @recorder(kind="t.leader.helper.fails")
    def fn(req):
        raise RuntimeError(f"boom-{req}")

    rec: Recorder = default_recorder
    job_id = _storage.new_id()
    now = _storage.now_iso()
    rec._insert_pending(job_id, "t.leader.helper.fails", None, now, json.dumps("x"))
    rec._execute(_storage.UPDATE_RUNNING, (now, job_id))
    row = rec._fetchone(
        f"SELECT {', '.join(_storage.COLUMNS)} FROM jobs WHERE id=?",
        (job_id,),
    )

    await _execute_claimed_row(rec, row)

    job = rec.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert isinstance(job.error, dict)
    assert job.error.get("type") == "RuntimeError"
    assert "boom-x" in job.error.get("message", "")


@pytest.mark.asyncio
async def test_execute_claimed_row_marks_unknown_kind_failed(recorder: Recorder):
    """A row whose kind isn't registered in this process gets `UnknownKind`."""
    job_id = _storage.new_id()
    now = _storage.now_iso()
    recorder._insert_pending(job_id, "t.leader.unknown", None, now, json.dumps(0))
    recorder._execute(_storage.UPDATE_RUNNING, (now, job_id))
    row = recorder._fetchone(
        f"SELECT {', '.join(_storage.COLUMNS)} FROM jobs WHERE id=?",
        (job_id,),
    )

    await _execute_claimed_row(recorder, row)

    job = recorder.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert isinstance(job.error, dict)
    assert job.error.get("type") == "UnknownKind"


@pytest.mark.asyncio
async def test_heartbeat_loop_refreshes_started_at(db_path):
    """One heartbeat tick must update `started_at` on the leader row."""
    rec = Recorder(path=db_path, leader_heartbeat_s=0.05)
    try:
        leader_id = rec._claim_leader_slot("host:helper-test")
        first = rec._fetchone("SELECT started_at FROM jobs WHERE id=?", (leader_id,))[0]

        shutdown = asyncio.Event()
        task = asyncio.create_task(_heartbeat_loop(rec, leader_id, shutdown))
        # One heartbeat (0.05s) plus a small margin.
        await asyncio.sleep(0.12)
        shutdown.set()
        await task

        second = rec._fetchone("SELECT started_at FROM jobs WHERE id=?", (leader_id,))[0]
        assert second > first
    finally:
        rec.shutdown()


@pytest.mark.asyncio
async def test_heartbeat_loop_reclaims_after_reaped(db_path, caplog):
    """If the row was reaped (status flipped to failed), the heartbeat
    loop re-claims a fresh slot rather than spinning silently. The
    resurrection emits dual-channel hazard surfacing — both
    `_logger.warning` and `warnings.warn(RecordedWarning)` per the
    `docs/HOW.md::Warnings policy`."""
    import warnings as _warnings

    from recorded import RecordedWarning

    rec = Recorder(path=db_path, leader_heartbeat_s=0.05, reaper_threshold_s=600.0)
    try:
        leader_id = rec._claim_leader_slot("host:reap-test")
        # Simulate a reaper sweep flipping the heartbeat row to failed.
        rec._mark_failed(leader_id, _storage.now_iso(), '{"type":"orphaned"}')

        shutdown = asyncio.Event()
        with (
            caplog.at_level("WARNING", logger="recorded"),
            _warnings.catch_warnings(record=True) as captured,
        ):
            _warnings.simplefilter("always", RecordedWarning)
            task = asyncio.create_task(_heartbeat_loop(rec, leader_id, shutdown))
            await asyncio.sleep(0.12)
            shutdown.set()
            await task

        # A fresh `running` heartbeat row should now exist. The reclaim
        # path uses the *current process's* host:pid (production shape:
        # the same leader process re-registers itself), so the new row's
        # key is the live `socket.gethostname():os.getpid()`, not the
        # test-seeded "host:reap-test" key.
        rows = rec._fetchall(
            "SELECT id, status FROM jobs WHERE kind=? AND status='running'",
            (_storage.LEADER_KIND,),
        )
        assert len(rows) == 1
        assert rows[0][0] != leader_id  # new id; the original is failed

        # Dual-channel hazard surfacing.
        log_msgs = [r.getMessage() for r in caplog.records if "re-claimed slot" in r.getMessage()]
        assert log_msgs, "expected logger.warning on resurrection"
        warn_msgs = [str(w.message) for w in captured if issubclass(w.category, RecordedWarning)]
        assert any("re-claimed slot" in m for m in warn_msgs), (
            f"expected RecordedWarning on resurrection, got: {warn_msgs}"
        )
    finally:
        rec.shutdown()
