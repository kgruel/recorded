"""Storage-layer behaviors: schema bootstrap, timestamp helper, IDs."""

from __future__ import annotations

import re
import sqlite3

from recorded import _storage


def test_now_iso_is_lex_sortable_iso8601_utc():
    s = _storage.now_iso()
    assert s.endswith("Z")
    # YYYY-MM-DDTHH:MM:SS.ffffffZ — fixed width, microseconds, Z suffix.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", s)


def test_now_iso_strings_sort_chronologically():
    a = _storage.now_iso()
    b = _storage.now_iso()
    # second call is at-or-after first
    assert a <= b


def test_new_id_is_unique_hex():
    ids = {_storage.new_id() for _ in range(1000)}
    assert len(ids) == 1000
    for i in ids:
        int(i, 16)  # parses as hex
        assert len(i) == 32  # uuid4().hex


def test_ensure_schema_creates_jobs_table_and_indexes(db_path):
    conn = _storage.open_connection(db_path)
    _storage.ensure_schema(conn)

    # table
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert cols == set(_storage.COLUMNS)

    # WAL mode active
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"

    # partial unique index for active rows is in place
    idx = {
        row[1]
        for row in conn.execute("PRAGMA index_list(jobs)").fetchall()
    }
    assert "idx_jobs_key_active" in idx
    assert "idx_jobs_status_kind" in idx
    assert "idx_jobs_kind_submitted" in idx


def test_partial_unique_index_blocks_two_active_rows_per_kind_key(db_path):
    conn = _storage.open_connection(db_path)
    _storage.ensure_schema(conn)

    conn.execute(
        _storage.INSERT_PENDING,
        ("id-1", "k.do", "key-A", _storage.now_iso(), None),
    )
    # second pending under same (kind, key) must collide
    try:
        conn.execute(
            _storage.INSERT_PENDING,
            ("id-2", "k.do", "key-A", _storage.now_iso(), None),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("expected IntegrityError on duplicate active key")


def test_partial_unique_index_permits_multiple_failed_rows_per_kind_key(db_path):
    """Failed history is preserved — not subject to the active-row uniqueness."""
    conn = _storage.open_connection(db_path)
    _storage.ensure_schema(conn)

    for n in range(3):
        conn.execute(
            "INSERT INTO jobs (id, kind, key, status, submitted_at, error_json) "
            "VALUES (?, ?, ?, 'failed', ?, ?)",
            (f"id-{n}", "k.do", "key-A", _storage.now_iso(), '{"type":"x"}'),
        )

    count = conn.execute(
        "SELECT count(*) FROM jobs WHERE kind=? AND key=? AND status='failed'",
        ("k.do", "key-A"),
    ).fetchone()[0]
    assert count == 3
