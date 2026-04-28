"""`python -m recorded run` — leader subprocess contract.

The leader process claims a `_recorded.leader` heartbeat row, refreshes
it periodically, and claims+executes pending rows from `.submit()`
callers (rows seeded directly here via `_insert_pending`). On
SIGTERM/SIGINT it drains in-flight tasks, releases the heartbeat slot,
and exits 0.

Pinned here:

- Heartbeat row appears within a bounded wait after process start.
- Pre-seeded `pending` rows of an imported kind reach `completed`.
- Async + sync kinds both execute under the leader.
- `attach()` ContextVar plumbing works in the leader.
- A failing function records `failed` with the right error shape.
- Graceful SIGTERM exits 0 and removes the heartbeat row.
- `--import` of an unknown module errors at parse-time (loud failure).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from recorded import Recorder, _storage

# Generous cap so CI flakiness doesn't masquerade as a leader hang.
SUBPROCESS_STARTUP_S = 5.0
SUBPROCESS_DRAIN_S = 5.0


def _spawn_leader(
    db_path: str,
    *,
    import_modules: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    """Spawn a leader subprocess with PYTHONPATH including tests/.

    The PYTHONPATH addition is what lets `--import _leader_kinds` resolve
    to `tests/_leader_kinds.py`.
    """
    env = os.environ.copy()
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    env["PYTHONPATH"] = tests_dir + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [sys.executable, "-m", "recorded", "run", "--path", db_path]
    for mod in import_modules or []:
        cmd.extend(["--import", mod])
    cmd.extend(extra_args or [])

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_for_leader(rec: Recorder, *, deadline_s: float = SUBPROCESS_STARTUP_S) -> None:
    """Poll until `is_leader_running()` returns True, or fail."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if rec.is_leader_running():
            return
        time.sleep(0.05)
    raise AssertionError(f"leader did not appear within {deadline_s}s")


def _wait_for_status(
    rec: Recorder,
    job_id: str,
    expected_status: str,
    *,
    deadline_s: float = SUBPROCESS_STARTUP_S,
) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        job = rec.get(job_id)
        if job is not None and job.status == expected_status:
            return
        time.sleep(0.02)
    job = rec.get(job_id)
    actual = job.status if job is not None else "<missing>"
    raise AssertionError(
        f"job {job_id} did not reach status={expected_status!r} within "
        f"{deadline_s}s (actual={actual!r})"
    )


def _terminate(proc: subprocess.Popen, *, drain_s: float = SUBPROCESS_DRAIN_S) -> int:
    """SIGTERM + wait. SIGKILL fallback if drain is exceeded."""
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=drain_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)
        raise


# ----- heartbeat lifecycle -------------------------------------------------


def test_leader_writes_heartbeat_row_on_startup(db_path):
    """A fresh leader subprocess registers itself in is_leader_running()."""
    rec = Recorder(path=db_path)
    proc = _spawn_leader(db_path, import_modules=["_leader_kinds"])
    try:
        _wait_for_leader(rec)
        # Heartbeat row exists with kind=LEADER_KIND.
        rows = rec._fetchall(
            "SELECT key, status FROM jobs WHERE kind=?",
            (_storage.LEADER_KIND,),
        )
        assert len(rows) == 1
        key, status = rows[0]
        assert ":" in key  # host:pid
        assert status == "running"
    finally:
        rc = _terminate(proc)
        rec.shutdown()
    assert rc == 0


def test_leader_releases_heartbeat_on_sigterm(db_path):
    """Graceful shutdown DELETEs the heartbeat row."""
    rec = Recorder(path=db_path)
    proc = _spawn_leader(db_path, import_modules=["_leader_kinds"])
    try:
        _wait_for_leader(rec)
        rc = _terminate(proc)
    finally:
        rec.shutdown()

    assert rc == 0
    rec2 = Recorder(path=db_path, reaper_threshold_s=600.0)
    try:
        rows = rec2._fetchall(
            "SELECT id FROM jobs WHERE kind=?",
            (_storage.LEADER_KIND,),
        )
        assert rows == []
        assert rec2.is_leader_running() is False
    finally:
        rec2.shutdown()


# ----- claim + execute ----------------------------------------------------


def _seed_pending(rec: Recorder, kind: str, request_json: str) -> str:
    job_id = _storage.new_id()
    rec._insert_pending(job_id, kind, None, _storage.now_iso(), request_json)
    return job_id


def test_leader_executes_sync_pending_row(db_path):
    rec = Recorder(path=db_path)
    proc = _spawn_leader(db_path, import_modules=["_leader_kinds"])
    try:
        _wait_for_leader(rec)
        job_id = _seed_pending(rec, "t.leader.echo", '"hello"')
        _wait_for_status(rec, job_id, "completed")
        job = rec.get(job_id)
        assert job is not None
        assert job.response == {"echoed": "hello"}
    finally:
        _terminate(proc)
        rec.shutdown()


def test_leader_executes_async_pending_row(db_path):
    rec = Recorder(path=db_path)
    proc = _spawn_leader(db_path, import_modules=["_leader_kinds"])
    try:
        _wait_for_leader(rec)
        job_id = _seed_pending(rec, "t.leader.echo_async", '"world"')
        _wait_for_status(rec, job_id, "completed")
        job = rec.get(job_id)
        assert job is not None
        assert job.response == {"echoed_async": "world"}
    finally:
        _terminate(proc)
        rec.shutdown()


def test_leader_records_failure_for_raising_function(db_path):
    rec = Recorder(path=db_path)
    proc = _spawn_leader(db_path, import_modules=["_leader_kinds"])
    try:
        _wait_for_leader(rec)
        job_id = _seed_pending(rec, "t.leader.fails", '"boom-payload"')
        _wait_for_status(rec, job_id, "failed")
        job = rec.get(job_id)
        assert job is not None
        assert isinstance(job.error, dict)
        assert job.error.get("type") == "RuntimeError"
        assert "boom-payload" in job.error.get("message", "")
    finally:
        _terminate(proc)
        rec.shutdown()


def test_leader_attach_propagates_in_subprocess(db_path):
    """`attach()` works on the leader side — ContextVar plumbing across
    process boundary is none of our business; the test confirms the
    leader's local plumbing matches today's worker."""
    rec = Recorder(path=db_path)
    proc = _spawn_leader(db_path, import_modules=["_leader_kinds"])
    try:
        _wait_for_leader(rec)
        job_id = _seed_pending(rec, "t.leader.attach_demo", '"value-x"')
        _wait_for_status(rec, job_id, "completed")
        job = rec.get(job_id)
        assert job is not None
        assert job.data == {"k": "value-x"}
    finally:
        _terminate(proc)
        rec.shutdown()


# ----- unknown kind --------------------------------------------------------


def test_leader_marks_unknown_kind_pending_row_failed(db_path):
    """A pending row whose kind isn't registered in the leader process gets
    flipped to `failed` with `{type: "UnknownKind"}` rather than
    stranding."""
    rec = Recorder(path=db_path)
    # Note: NOT importing _leader_kinds, so t.leader.echo is unknown to leader.
    proc = _spawn_leader(db_path)
    try:
        _wait_for_leader(rec)
        job_id = _seed_pending(rec, "t.leader.echo", '"hi"')
        _wait_for_status(rec, job_id, "failed")
        job = rec.get(job_id)
        assert job is not None
        assert isinstance(job.error, dict)
        assert job.error.get("type") == "UnknownKind"
        assert "t.leader.echo" in job.error.get("message", "")
    finally:
        _terminate(proc)
        rec.shutdown()


# ----- argparse failure modes ---------------------------------------------


def test_run_with_unknown_import_module_fails_fast(db_path):
    """`--import package.does_not_exist` surfaces ModuleNotFoundError loudly."""
    proc = _spawn_leader(db_path, import_modules=["_definitely_not_a_module_xyz"])
    try:
        rc = proc.wait(timeout=SUBPROCESS_STARTUP_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("run command should have exited on missing import")
    assert rc != 0
    stderr = proc.stderr.read() if proc.stderr else ""
    assert "ModuleNotFoundError" in stderr or "_definitely_not_a_module_xyz" in stderr
