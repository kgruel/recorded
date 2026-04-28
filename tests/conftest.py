"""Shared fixtures.

Real SQLite, file-backed (not `:memory:`) so the setup matches how the
library is used in production and how phase-2 multi-connection tests
will exercise the worker.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from recorded import Recorder, _storage
from recorded import _recorder as _recorder_mod
from recorded import _registry as _registry_mod


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "jobs.db")


@pytest.fixture
def recorder(db_path):
    r = Recorder(path=db_path)
    try:
        yield r
    finally:
        r.shutdown()


@pytest.fixture
def default_recorder(recorder):
    """Install `recorder` as the module-level default for the test."""
    _recorder_mod._set_default_for_testing(recorder)
    try:
        yield recorder
    finally:
        _recorder_mod._set_default_for_testing(None)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the kind registry between tests so adapters don't leak."""
    yield
    _registry_mod._reset()


# ---------------------------------------------------------------------------
# Leader-subprocess fixture
# ---------------------------------------------------------------------------
#
# Used by tests that exercise `.submit()` end-to-end under cross-process
# leadership. The fixture spawns `python -m recorded run --path <db_path>
# --import _leader_kinds` as a subprocess with `PYTHONPATH=tests/`,
# waits until `is_leader_running()` returns True, and yields a Recorder
# wired as the module default.
#
# Tests that don't need a leader keep using `default_recorder` / `recorder`.
# Once the worker is removed (PLAN.md step 6), `.submit()` requires a
# leader; tests using `default_recorder` for `.submit()` will fail at the
# `.submit()` gate and need to be moved here.

LEADER_STARTUP_TIMEOUT_S = 5.0
LEADER_DRAIN_TIMEOUT_S = 5.0


def _spawn_leader_subprocess(db_path: str) -> subprocess.Popen:
    env = os.environ.copy()
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    env["PYTHONPATH"] = tests_dir + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable,
        "-m",
        "recorded",
        "run",
        "--path",
        db_path,
        "--import",
        "_leader_kinds",
        "--shutdown-timeout",
        "3.0",
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_for_leader(rec: Recorder, *, deadline_s: float = LEADER_STARTUP_TIMEOUT_S) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if rec.is_leader_running():
            return
        time.sleep(0.05)
    raise AssertionError(f"leader subprocess did not register heartbeat within {deadline_s}s")


def _terminate_leader(proc: subprocess.Popen) -> None:
    if proc.returncode is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=LEADER_DRAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)


@pytest.fixture
def leader_recorder(db_path):
    """Recorder against `db_path` with a leader subprocess running.

    Re-registers `_leader_kinds`' canonical kinds in this process so test
    code can `from _leader_kinds import echo` and use it as a normal
    `@recorder`-decorated wrapper. The registry restoration is needed
    because `_clean_registry` (autouse) resets between tests but the
    module's `@recorder` decorations only run on first import.
    """
    # Local import: ensures `tests/` is on sys.path when this fixture runs
    # (pytest adds it for the test files; the subprocess gets it via the
    # PYTHONPATH set in `_spawn_leader_subprocess`).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import _leader_kinds  # noqa: PLC0415 — fixture-time sys.path init
    finally:
        sys.path.pop(0)
    _leader_kinds._ensure_registered()

    rec = Recorder(path=db_path)
    _recorder_mod._set_default_for_testing(rec)
    proc = _spawn_leader_subprocess(db_path)
    try:
        _wait_for_leader(rec)
        yield rec
    finally:
        _terminate_leader(proc)
        _recorder_mod._set_default_for_testing(None)
        rec.shutdown()
        # Verify graceful release (best-effort) — not asserted, since the
        # subprocess may have been SIGKILL'd if drain timed out.
        rec2 = Recorder(path=db_path, reaper_threshold_s=600.0)
        try:
            stale_or_gone = not rec2.is_leader_running()
            if not stale_or_gone:
                # Subprocess didn't release cleanly. Tests assert their own
                # invariants; this is just diagnostic logging into stderr.
                rows = rec2._fetchall(
                    "SELECT key, started_at FROM jobs WHERE kind=?",
                    (_storage.LEADER_KIND,),
                )
                print(
                    f"warning: leader_recorder teardown saw lingering heartbeat rows: {rows}",
                    file=sys.stderr,
                )
        finally:
            rec2.shutdown()
