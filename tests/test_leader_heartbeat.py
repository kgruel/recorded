"""Leader-heartbeat protocol contract: `_recorded.leader` rows.

The leader process (`python -m recorded run`) inserts a single
`_recorded.leader` row keyed by `host:pid` on startup, and updates its
`started_at` periodically as a liveness signal. `Recorder.is_leader_running()`
checks for any fresh row of this kind; `.submit()` will gate on that
(step 5 of the worker dissolution).

Pinned here:

- `_claim_leader_slot(host_pid)` inserts the row with the right shape.
- `is_leader_running()` is True while fresh, False once stale or absent.
- `_touch_leader_heartbeat(id)` refreshes `started_at`, returns True
  while the row is still ours.
- A reaper-flipped heartbeat causes `_touch_leader_heartbeat` to return
  False (the leader notices and re-claims a slot — resurrection edge).
- Restart with same `host:pid` (after a prior heartbeat row was
  abandoned) replaces the prior row cleanly.
- Multiple distinct `host:pid` leaders coexist.
- `_release_leader_slot` is the graceful-shutdown counterpart.
- `_recorded.leader` rows do not appear in default `query()`/`last()`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import recorded
from recorded import Recorder, _storage


def _stale_iso(seconds: float) -> str:
    """ISO timestamp `seconds` in the past — deterministic staleness."""
    return _storage.format_iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


# ----- claim shape ----------------------------------------------------------


def test_claim_leader_slot_inserts_running_row(recorder: Recorder):
    """Named test: `_claim_leader_slot('host:1')` writes the right row."""
    leader_id = recorder._claim_leader_slot("host:1")

    row = recorder._fetchone(
        "SELECT kind, key, status, started_at FROM jobs WHERE id=?",
        (leader_id,),
    )
    assert row is not None
    kind, key, status, started_at = row
    assert kind == _storage.LEADER_KIND
    assert key == "host:1"
    assert status == "running"
    assert started_at is not None


def test_is_leader_running_false_when_no_heartbeat(recorder: Recorder):
    """No heartbeat row → no leader."""
    assert recorder._is_leader_running() is False


def test_is_leader_running_true_after_claim(recorder: Recorder):
    """A freshly-claimed slot is observable as a running leader."""
    recorder._claim_leader_slot("host:42")
    assert recorder._is_leader_running() is True


def test_public_is_leader_running_alias(recorder: Recorder):
    """`Recorder.is_leader_running()` is the public alias of the underscore method."""
    assert recorder.is_leader_running() is False
    recorder._claim_leader_slot("host:42")
    assert recorder.is_leader_running() is True


# ----- staleness ------------------------------------------------------------


def test_is_leader_running_false_when_heartbeat_is_stale(db_path):
    """A `_recorded.leader` row with old `started_at` is not "fresh"."""
    rec = Recorder(path=db_path, leader_stale_s=2.0, reaper_threshold_s=600.0)
    try:
        # Manually insert a leader row with started_at well outside the
        # 2.0s staleness window.
        leader_id = _storage.new_id()
        rec._insert_running(
            leader_id,
            _storage.LEADER_KIND,
            "host:dead",
            _stale_iso(120.0),
            _stale_iso(120.0),
            None,
        )
        assert rec._is_leader_running() is False
    finally:
        rec.shutdown()


def test_touch_leader_heartbeat_refreshes_started_at(recorder: Recorder):
    """`_touch_leader_heartbeat` writes a new `started_at` and returns True."""
    # Seed a row whose `started_at` is intentionally old, then touch.
    leader_id = _storage.new_id()
    recorder._insert_running(
        leader_id,
        _storage.LEADER_KIND,
        "host:touch",
        _stale_iso(60.0),
        _stale_iso(60.0),
        None,
    )

    refreshed = recorder._touch_leader_heartbeat(leader_id)
    assert refreshed is True

    row = recorder._fetchone(
        "SELECT started_at FROM jobs WHERE id=?",
        (leader_id,),
    )
    # New started_at must be much more recent than the seeded 60s-stale one.
    new_started_at = row[0]
    assert new_started_at > _stale_iso(10.0)


def test_touch_leader_heartbeat_returns_false_for_reaped_row(recorder: Recorder):
    """If the row was flipped to `failed` (e.g. by the reaper while we
    were unresponsive), the conditional UPDATE no-ops. The leader uses
    this signal to re-claim a fresh slot — the resurrection edge."""
    leader_id = _storage.new_id()
    now = _storage.now_iso()
    recorder._insert_running(
        leader_id,
        _storage.LEADER_KIND,
        "host:reaped",
        now,
        now,
        None,
    )
    # Simulate a reaper sweep flipping the row to failed.
    recorder._mark_failed(leader_id, _storage.now_iso(), '{"type":"orphaned"}')

    refreshed = recorder._touch_leader_heartbeat(leader_id)
    assert refreshed is False


# ----- multi-leader coexistence + restart ----------------------------------


def test_multiple_leader_pids_coexist(recorder: Recorder):
    """Two leaders running on different (host, pid) pairs both register."""
    id_a = recorder._claim_leader_slot("host-a:1")
    id_b = recorder._claim_leader_slot("host-b:2")
    assert id_a != id_b
    assert recorder._is_leader_running() is True

    rows = recorder._fetchall(
        "SELECT key FROM jobs WHERE kind=? AND status='running'",
        (_storage.LEADER_KIND,),
    )
    keys = sorted(k for (k,) in rows)
    assert keys == ["host-a:1", "host-b:2"]


def test_claim_leader_slot_replaces_stale_same_host_pid(recorder: Recorder):
    """Restart-with-same-(host,pid): the prior row is replaced cleanly,
    no IntegrityError surfaces. The partial unique index forbids two
    active rows with the same `(kind, key)`; `_claim_leader_slot` resolves
    the collision by DELETing the prior row first.
    """
    # First "process" claims.
    first_id = recorder._claim_leader_slot("host:reuse")

    # Second "process" comes up with the same identity — the prior row is
    # still active. Must succeed without raising.
    second_id = recorder._claim_leader_slot("host:reuse")
    assert second_id != first_id

    # Only one active row remains (the second one).
    rows = recorder._fetchall(
        "SELECT id FROM jobs WHERE kind=? AND key=? AND status='running'",
        (_storage.LEADER_KIND, "host:reuse"),
    )
    assert [r[0] for r in rows] == [second_id]


# ----- release --------------------------------------------------------------


def test_release_leader_slot_deletes_row(recorder: Recorder):
    """Graceful shutdown: `_release_leader_slot` removes the row entirely."""
    leader_id = recorder._claim_leader_slot("host:graceful")
    assert recorder._is_leader_running() is True

    recorder._release_leader_slot(leader_id)
    assert recorder._is_leader_running() is False

    row = recorder._fetchone("SELECT 1 FROM jobs WHERE id=?", (leader_id,))
    assert row is None


# ----- read-API exclusion (sanity check) -----------------------------------


def test_heartbeat_rows_excluded_from_default_query(default_recorder):
    """A live heartbeat row does NOT appear in `recorded.query()` results
    when the caller didn't ask for `_recorded.*` explicitly. Step 1
    pinned this against a synthetic seed row; this test confirms it
    against an actual `_claim_leader_slot`-inserted row."""
    default_recorder._claim_leader_slot("host:filter")

    rows = list(recorded.query())
    assert all(not j.kind.startswith("_recorded.") for j in rows)

    # Explicit opt-in surfaces it.
    leader_rows = list(recorded.query(kind="_recorded.*"))
    assert len(leader_rows) == 1
    assert leader_rows[0].kind == _storage.LEADER_KIND
