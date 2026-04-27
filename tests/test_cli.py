"""`python -m recorded` CLI tests via real subprocess invocation.

The DB is pre-seeded inside the test by writing rows through a `Recorder`
on the same path, then shutting it down so the subprocess sees the
committed state.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from recorded import Recorder, recorder
from recorded import _recorder as _recorder_mod


# Cap every CLI invocation so a bug can't hang CI.
SUBPROCESS_TIMEOUT_S = 2.0


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "recorded", *args],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )


@pytest.fixture
def seeded_db_path(tmp_path):
    """Open a Recorder, seed completed/failed rows, shut it down, and
    return the path. The autouse `_clean_registry` fixture in conftest
    resets the kind registry afterward.
    """
    db_path = str(tmp_path / "jobs.db")
    r = Recorder(path=db_path)
    _recorder_mod._set_default(r)
    try:
        import asyncio

        @recorder(kind="broker.place_order")
        async def place(req):
            return {"ok": True, "req": req}

        @recorder(kind="broker.cancel_order")
        async def cancel(req):
            return {"cancelled": True, "req": req}

        @recorder(kind="broker.flaky")
        async def flaky(req):
            raise RuntimeError("boom")

        async def seed():
            # 3 places (one keyed), 2 cancels, 2 flaky failures.
            await place({"i": 1})
            await place({"i": 2}, key="k1")
            await place({"i": 3})
            await cancel({"i": 4})
            await cancel({"i": 5})
            for _ in range(2):
                try:
                    await flaky({"i": 6})
                except RuntimeError:
                    pass

        asyncio.run(seed())
    finally:
        _recorder_mod._set_default(None)
        r.shutdown()
    return db_path


# ----- last ---------------------------------------------------------------


def test_cli_last_prints_n_completed_jobs_descending(seeded_db_path):
    """Named test: `last` returns and formats rows; ordering correct."""
    proc = _run_cli("last", "3", "--path", seeded_db_path)
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 3

    # Descending by submitted_at: each line's leading timestamp >= next.
    timestamps = [l.split()[0] for l in lines]
    assert timestamps == sorted(timestamps, reverse=True)

    # One-line format: timestamp, status, kind, id-prefix, key=...
    first = lines[0].split()
    # status field is column 1
    assert first[1] in ("completed", "failed", "pending", "running")
    assert "key=" in lines[0]


def test_cli_last_filters_by_kind_glob_and_status(seeded_db_path):
    """Named test: global filters compose."""
    proc = _run_cli(
        "last",
        "20",
        "--kind",
        "broker.*",
        "--status",
        "failed",
        "--path",
        seeded_db_path,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    # 2 failed flaky rows.
    assert len(lines) == 2
    for l in lines:
        # status column is the 2nd whitespace-separated token.
        assert l.split()[1] == "failed"
        assert "broker.flaky" in l


# ----- get ----------------------------------------------------------------


def test_cli_get_prints_job_json(seeded_db_path):
    """Named test: `get <id>` happy path."""
    # Pull an id via `last 1`.
    last_proc = _run_cli("last", "1", "--path", seeded_db_path)
    short_id = last_proc.stdout.split()[3]  # 4th column

    # Use sqlite directly to find the full id (last only emits a prefix).
    import sqlite3

    conn = sqlite3.connect(seeded_db_path)
    full_id = conn.execute(
        "SELECT id FROM jobs WHERE id LIKE ? LIMIT 1", (short_id + "%",)
    ).fetchone()[0]
    conn.close()

    proc = _run_cli("get", full_id, "--path", seeded_db_path)
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout)
    assert payload["id"] == full_id
    assert payload["status"] in ("completed", "failed")
    assert "kind" in payload
    assert "request" in payload


def test_cli_get_with_prompt_flag_emits_to_prompt_output(seeded_db_path):
    """Named test: `--prompt` switches output mode."""
    last_proc = _run_cli("last", "1", "--path", seeded_db_path)
    short_id = last_proc.stdout.split()[3]

    import sqlite3

    conn = sqlite3.connect(seeded_db_path)
    full_id = conn.execute(
        "SELECT id FROM jobs WHERE id LIKE ? LIMIT 1", (short_id + "%",)
    ).fetchone()[0]
    conn.close()

    proc = _run_cli("get", full_id, "--prompt", "--path", seeded_db_path)
    assert proc.returncode == 0, proc.stderr

    # Markdown shape: starts with `# kind — status`, has a Request block.
    out = proc.stdout
    assert out.startswith("# ")
    assert "## Request" in out
    assert "```json" in out


def test_cli_get_unknown_id_exits_2_with_stderr_message(seeded_db_path):
    """Named test: exit code + stderr."""
    proc = _run_cli("get", "deadbeef" * 4, "--path", seeded_db_path)
    assert proc.returncode == 2
    assert "no job" in proc.stderr.lower()
    assert proc.stdout == ""


# ----- tail ---------------------------------------------------------------


def test_cli_tail_emits_new_terminal_rows_after_watermark(tmp_path):
    """Named test: poll loop sees new rows; doesn't replay history."""
    db_path = str(tmp_path / "jobs.db")

    # Pre-existing rows that should NOT show up in tail (created before launch).
    r0 = Recorder(path=db_path)
    _recorder_mod._set_default(r0)
    try:
        import asyncio

        @recorder(kind="t.tail.pre")
        async def pre(req):
            return {"r": req}

        async def s():
            await pre(1)
            await pre(2)

        asyncio.run(s())
    finally:
        _recorder_mod._set_default(None)
        r0.shutdown()

    # Launch tail with a snappy interval.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "recorded",
            "tail",
            "--path",
            db_path,
            "--interval",
            "0.05",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Give tail time to enter its poll loop.
        time.sleep(0.2)

        # Now write NEW rows after tail's watermark was set.
        r1 = Recorder(path=db_path)
        _recorder_mod._set_default(r1)
        try:
            import asyncio

            @recorder(kind="t.tail.post")
            async def post(req):
                return {"r": req}

            async def s():
                await post(10)
                await post(11)

            asyncio.run(s())
        finally:
            _recorder_mod._set_default(None)
            r1.shutdown()

        # Wait for the poll loop to flush.
        time.sleep(0.4)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=SUBPROCESS_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            pytest.fail(f"tail did not exit on SIGINT; stderr={stderr!r}")

    assert proc.returncode == 0, f"stderr={stderr!r}"
    # Pre-existing rows must not appear; new rows must.
    assert "t.tail.pre" not in stdout
    assert "t.tail.post" in stdout
    new_lines = [l for l in stdout.splitlines() if "t.tail.post" in l]
    assert len(new_lines) == 2


def test_cli_tail_handles_keyboard_interrupt_with_exit_zero(tmp_path):
    """Named test: Ctrl-C path exits cleanly."""
    db_path = str(tmp_path / "jobs.db")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "recorded",
            "tail",
            "--path",
            db_path,
            "--interval",
            "0.05",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.15)
    proc.send_signal(signal.SIGINT)
    try:
        _, stderr = proc.communicate(timeout=SUBPROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("tail did not exit on SIGINT")

    assert proc.returncode == 0
    # No Python traceback leaked.
    assert "Traceback" not in stderr


# ----- --path -------------------------------------------------------------


def test_cli_path_flag_targets_an_explicit_db_file(tmp_path):
    """Named test: `--path` overrides default."""
    custom = str(tmp_path / "custom_jobs.db")

    r = Recorder(path=custom)
    _recorder_mod._set_default(r)
    try:
        import asyncio

        @recorder(kind="t.path")
        async def fn(req):
            return {"r": req}

        asyncio.run(fn(99))
    finally:
        _recorder_mod._set_default(None)
        r.shutdown()

    # Run from a working directory where ./jobs.db doesn't exist; if --path
    # weren't honored, the CLI would either find nothing or create a stray
    # ./jobs.db. We just assert the row from `custom` is what we see.
    proc = subprocess.run(
        [sys.executable, "-m", "recorded", "last", "10", "--path", custom],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert "t.path" in proc.stdout

    # A separate empty path should produce no rows.
    empty = str(tmp_path / "empty.db")
    proc2 = subprocess.run(
        [sys.executable, "-m", "recorded", "last", "10", "--path", empty],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    assert proc2.returncode == 0
    assert proc2.stdout.strip() == ""

    # Cleanup the auto-created empty.db file.
    if os.path.exists(empty):
        os.remove(empty)
