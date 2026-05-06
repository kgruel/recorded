from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import recorded


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _insert_row(conn, *, job_id: str, kind: str, status: str, submitted_at: str):
    conn.execute(
        "INSERT INTO jobs "
        "(id, kind, key, status, submitted_at, started_at, completed_at, request_json) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
        (
            job_id,
            kind,
            status,
            submitted_at,
            submitted_at if status in {"running", "completed", "failed"} else None,
            submitted_at if status in {"completed", "failed"} else None,
            json.dumps({"x": 1}),
        ),
    )


def test_health_empty_db(default_recorder):
    h = recorded.health()
    assert h["total_rows"] == 0
    assert h["oldest_row"] is None
    assert h["newest_row"] is None
    assert h["rows_last_hour"] == 0
    assert h["last_failed_at"] is None
    assert isinstance(h["db_size_mb"], float)
    assert h["db_size_mb"] >= 0.0
    assert h["leader_running"] is False


def test_health_populated_mixed_rows_includes_reserved(default_recorder):
    conn = default_recorder.connection()
    now = datetime.now(timezone.utc)

    oldest = _iso(now - timedelta(hours=3))
    last_hour_1 = _iso(now - timedelta(minutes=45))
    last_hour_2 = _iso(now - timedelta(minutes=15))
    newest = _iso(now - timedelta(minutes=1))

    _insert_row(conn, job_id="j1", kind="user.alpha", status="completed", submitted_at=oldest)
    _insert_row(conn, job_id="j2", kind="user.beta", status="failed", submitted_at=last_hour_1)
    _insert_row(
        conn,
        job_id="j3",
        kind="_recorded.leader",
        status="running",
        submitted_at=last_hour_2,
    )
    _insert_row(conn, job_id="j4", kind="user.gamma", status="completed", submitted_at=newest)

    h = recorded.health()

    assert h["total_rows"] == 4
    assert h["oldest_row"] == oldest
    assert h["newest_row"] == newest
    assert h["rows_last_hour"] == 3
    assert h["last_failed_at"] == last_hour_1
