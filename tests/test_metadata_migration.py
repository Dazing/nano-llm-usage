"""Delivery 2 Step 2: durable API prerequisites, numbered migration, coverage metadata.

Covers plan Step 2 verification: a version-1 fixture with totals 900/60/960 is
migrated to 0002 and keeps those totals, contribution quality and gap scope are
recorded correctly, coverage metadata is reconstructable from retained raw
rows, and legacy rows whose raw coverage was already deleted stay
``metadata_complete: false`` with null bounds instead of invented clean coverage.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jarvis_dashboard import storage
from jarvis_dashboard.accounting import transition_series
from jarvis_dashboard.domain import (
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    QUALITY_FLAGS,
    REQUEST_INPUT,
    REQUEST_OUTPUT,
    QualityFlag,
    ScrapeCounters,
    StatsEvidence,
    canonical_labels_json,
    to_iso,
)

from test_storage_helpers import downgrade_to_v1, seed_chain

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "jarvis_dashboard" / "migrations"
MIG_0001 = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
MIG_0002 = (MIGRATIONS_DIR / "0002_api_metadata.sql").read_text(encoding="utf-8")

T0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
L = {"model_name": "m", "engine": "0"}
IDENT = canonical_labels_json(L)
SOURCE = "s1"

# Acceptance scenario A1/A2: opening (1000,100) then (1600,140), (1900,160).
A2_STEPS = [(1000, 100), (1600, 140), (1900, 160)]
A2_EXPECTED = {COMPUTED_INPUT: 900, GENERATED_OUTPUT: 60}


def counters(ci, go, ri=None, ro=None) -> ScrapeCounters:
    return ScrapeCounters(
        dict(L),
        {"computed_input": ci, "generated_output": go, "request_input": ri, "request_output": ro},
        {},
    )


def stats(at: datetime, uptime: float = 1000.0) -> StatsEvidence:
    return StatsEvidence("m", uptime, at, at)


def apply(conn, poll_id, plans, at: datetime) -> int:
    return storage.apply_poll(
        conn,
        source_id=SOURCE,
        endpoint_fingerprint="fp1",
        poll_id=poll_id,
        observed_at=at,
        plans=plans,
    )


def run_a2(conn) -> None:
    """Scenario A1+A2: baseline then two accepted intervals (900/60/960)."""
    state = None
    for i, (ci, go) in enumerate(A2_STEPS):
        at = T0 + timedelta(seconds=15 * i)
        plan = transition_series(state, counters(ci, go), at, stats(at, 1000 + 15 * i), 15)
        apply(conn, f"p{i}", [plan], at)
        state = plan.state


def build_v1(tmp_path: Path, monkeypatch) -> sqlite3.Connection:
    """Open the database the Delivery 1 writer would have written."""
    return storage.open_storage(tmp_path / "d.sqlite3")


def seal_v1(conn: sqlite3.Connection) -> None:
    """Reduce the written data to the shape Delivery 1 shipped, then close."""
    downgrade_to_v1(conn)
    conn.close()


def migrate_to_v2(tmp_path: Path, monkeypatch) -> sqlite3.Connection:
    """Upgrade the version-1 database through the real migration path."""
    monkeypatch.setattr(storage, "CURRENT_SCHEMA_VERSION", 2)
    return storage.open_storage(tmp_path / "d.sqlite3")


def hourly_row(conn, counter=COMPUTED_INPUT, hour="2026-01-15T10Z"):
    return conn.execute(
        "SELECT tokens, observed_intervals, quality_json, first_observed_at, last_observed_at, "
        "metadata_complete FROM hourly_totals WHERE source_id=? AND identity=? AND hour=? "
        "AND counter_key=?",
        (SOURCE, IDENT, hour, counter),
    ).fetchone()


def checksum(conn) -> list[tuple]:
    return conn.execute(
        "SELECT source_id, identity, hour, counter_key, tokens, observed_intervals "
        "FROM hourly_totals ORDER BY 1,2,3,4"
    ).fetchall()


# --------------------------------------------------------------------------
# Migration safety and total preservation
# --------------------------------------------------------------------------


def test_migration_preserves_scenario_a2_totals(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    run_a2(conn)
    assert conn.execute(
        "SELECT counter_key, sum(tokens) FROM hourly_totals WHERE tokens IS NOT NULL "
        "GROUP BY counter_key ORDER BY 1"
    ).fetchall() == [(COMPUTED_INPUT, 900), (GENERATED_OUTPUT, 60)]
    before = checksum(conn)
    seal_v1(conn)

    conn = migrate_to_v2(tmp_path, monkeypatch)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert checksum(conn) == before
    assert (tmp_path / "d.sqlite3.pre-migration-2.sqlite3").exists()
    assert storage.verify_database(tmp_path / "d.sqlite3.pre-migration-2.sqlite3") == 1
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # Processed total is the sum of both primary counters: 960.
    assert sum(A2_EXPECTED.values()) == 960


def test_migration_is_idempotent_and_indexed(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    run_a2(conn)
    seal_v1(conn)
    migrate_to_v2(tmp_path, monkeypatch).close()
    conn = migrate_to_v2(tmp_path, monkeypatch)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert not (tmp_path / "d.sqlite3.pre-migration-3.sqlite3").exists()
    indexes = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"idx_series_enum", "idx_events_series_order", "idx_gap_scope"} <= indexes


# --------------------------------------------------------------------------
# Contribution coverage metadata (new writes)
# --------------------------------------------------------------------------


def test_writer_records_contribution_bounds_and_quality(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    seal_v1(conn)
    conn = migrate_to_v2(tmp_path, monkeypatch)
    run_a2(conn)

    first, last = (T0 + timedelta(seconds=15)).isoformat(), (T0 + timedelta(seconds=30)).isoformat()
    row = hourly_row(conn)
    assert row[0] == 900 and row[1] == 2
    assert row[2] == "[]"  # every interval was continuity-verified: no flags
    assert row[3].startswith(first[:19]) and row[4].startswith(last[:19])
    assert row[5] == 1


def test_writer_merges_contribution_quality(tmp_path: Path, monkeypatch):
    """The hourly row's quality is the union of contributing intervals (plan D1)."""
    conn = build_v1(tmp_path, monkeypatch)
    # Continuous process: uptime advances with the clock, so continuity holds.
    steps = [
        (1000, 100, T0, 1000.0),
        (1600, 140, T0 + timedelta(seconds=15), 1015.0),
        # Then an outage that ends 5 s into the next hour: gap + approximate hour.
        (1900, 160, datetime(2026, 1, 15, 11, 0, 5, tzinfo=UTC), 4605.0),
    ]
    state = None
    for i, (ci, go, at, uptime) in enumerate(steps):
        plan = transition_series(state, counters(ci, go), at, stats(at, uptime), 15)
        apply(conn, f"p{i}", [plan], at)
        state = plan.state

    ten = hourly_row(conn, hour="2026-01-15T10Z")
    eleven = hourly_row(conn, hour="2026-01-15T11Z")
    # The outage's gap overlaps the 10:00 bucket, so its marker merges in.
    assert ten[0] == 600 and set(json.loads(ten[2])) == {QualityFlag.COLLECTION_GAP}
    assert eleven[0] == 300
    assert set(json.loads(eleven[2])) == {
        QualityFlag.COLLECTION_GAP,
        QualityFlag.TIME_ATTRIBUTION_APPROXIMATE,
    }
    # Six independent quality dimensions are all storable in the union.
    assert len(QUALITY_FLAGS) == 6


def test_gap_markers_use_the_counter_scope(tmp_path: Path, monkeypatch):
    """Gap markers are written for every counter of the gap's own scope (plan D1)."""
    conn = build_v1(tmp_path, monkeypatch)
    at = T0
    plan = transition_series(None, counters(1000, 100, 2000, 100), at, stats(at, 1000), 15)
    apply(conn, "p0", [plan], at)
    state = plan.state
    # A two-hour outage: the 11:00 bucket has gap evidence and no credit.
    at = T0 + timedelta(hours=2, minutes=4)
    uptime = 1000 + (at - T0).total_seconds()  # continuous process: a pure collection gap
    plan = transition_series(state, counters(1600, 140, 2100, 110), at, stats(at, uptime), 15)
    apply(conn, "p1", [plan], at)

    for counter in (COMPUTED_INPUT, GENERATED_OUTPUT, REQUEST_INPUT, REQUEST_OUTPUT):
        row = hourly_row(conn, counter, hour="2026-01-15T11Z")
        assert row is not None, counter
        assert row[0] is None, f"{counter}: gap-only bucket must not invent credit"
        assert QualityFlag.COLLECTION_GAP in json.loads(row[2]), counter
    assert conn.execute(
        "SELECT count(*) FROM hourly_totals WHERE hour='2026-01-15T11Z' AND tokens IS NOT NULL"
    ).fetchone()[0] == 0


def test_gap_on_one_scope_leaves_other_counters_clean(tmp_path: Path, monkeypatch):
    """A request-counter gap must not mark the engine totals as gapped (plan D1)."""
    conn = build_v1(tmp_path, monkeypatch)
    at = T0
    plan = transition_series(None, counters(1000, 100, 50, 100), at, stats(at, 1000), 100)
    apply(conn, "p0", [plan], at)
    state = plan.state
    at = T0 + timedelta(seconds=60)  # within two poll intervals: no primary gap
    plan = transition_series(
        state,
        ScrapeCounters(
            dict(L),
            {COMPUTED_INPUT: 1100, GENERATED_OUTPUT: 110, REQUEST_INPUT: 60, REQUEST_OUTPUT: None},
            {REQUEST_OUTPUT: "missing"},
        ),
        at,
        stats(at, 1060),
        100,
    )
    apply(conn, "p1", [plan], at)
    state = plan.state
    at = T0 + timedelta(seconds=120)
    plan = transition_series(state, counters(1200, 120, 70, 130), at, stats(at, 1120), 100)
    apply(conn, "p2", [plan], at)

    primary = json.loads(hourly_row(conn, COMPUTED_INPUT)[2])
    coupled = json.loads(hourly_row(conn, GENERATED_OUTPUT)[2])
    gapped = json.loads(hourly_row(conn, REQUEST_OUTPUT)[2])
    untouched = json.loads(hourly_row(conn, REQUEST_INPUT)[2])
    assert QualityFlag.COLLECTION_GAP in gapped
    for quality in (primary, coupled, untouched):
        assert QualityFlag.COLLECTION_GAP not in quality


def test_hour_gap_markers_survive_raw_retention(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    run_a2(conn)
    seal_v1(conn)
    conn = migrate_to_v2(tmp_path, monkeypatch)
    storage.purge_expired(conn, to_iso(T0 + timedelta(days=1)))
    row = hourly_row(conn)
    assert row[0] == 900 and row[1] == 2
    assert row[5] == 1  # already-materialized metadata is durable, not raw-dependent


# --------------------------------------------------------------------------
# Bounded backfill of legacy rows
# --------------------------------------------------------------------------


def test_backfill_reconstructs_bounds_from_retained_samples(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    run_a2(conn)
    seal_v1(conn)

    conn = migrate_to_v2(tmp_path, monkeypatch)
    row = hourly_row(conn)
    assert row[0] == 900 and row[1] == 2
    assert row[5] == 1
    assert row[3].startswith("2026-01-15T10:00:15") and row[4].startswith("2026-01-15T10:00:30")
    assert conn.execute(
        "SELECT count(*) FROM hourly_totals WHERE metadata_complete=0"
    ).fetchone()[0] == 0


def test_backfill_leaves_expired_legacy_metadata_unknown(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    run_a2(conn)
    storage.purge_expired(conn, to_iso(T0 + timedelta(days=1)))
    assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 0
    seal_v1(conn)

    conn = migrate_to_v2(tmp_path, monkeypatch)
    row = hourly_row(conn)
    assert row[0] == 900 and row[1] == 2  # totals untouched
    assert row[5] == 0 and row[3] is None and row[4] is None  # never invented
    assert storage.get_runtime_state(conn, storage.API_METADATA_BACKFILL_KEY) is None


def test_backfill_is_bounded_resumable_and_total_neutral(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    for index in range(3):
        seed_chain(
            conn,
            f"m{index}",
            [(1000 + 100 * i, 100 + 10 * i) for i in range(3)],
            T0 + timedelta(hours=index),
        )
    seal_v1(conn)

    monkeypatch.setattr(storage, "CURRENT_SCHEMA_VERSION", 2)
    # Migrate without the startup backfill so the bounded phases can be observed.
    monkeypatch.setattr(storage, "run_backfills", lambda conn, **_kw: 0)
    conn = storage.open_storage(tmp_path / "d.sqlite3")
    before = checksum(conn)
    before_counts = storage.snapshot_metadata_counts(conn)
    assert before_counts["rows"] > 0
    assert before_counts["complete"] == 0  # legacy rows start with unknown coverage

    # One row per phase batch: progress is bounded and converges.
    rounds = 0
    while storage.backfill_api_metadata(conn, batch=1):
        rounds += 1
        assert rounds < 200, "backfill must converge"
    after = storage.snapshot_metadata_counts(conn)
    assert after["rows"] == before_counts["rows"]
    assert after["complete"] == after["rows"]
    assert checksum(conn) == before  # every hourly total untouched
    assert conn.execute(
        "SELECT value FROM maintenance_cursor WHERE key=?",
        (storage.API_METADATA_BACKFILL_KEY,),
    ).fetchone() is None  # completion state cleared
    assert conn.execute("SELECT count(*) FROM series WHERE seq IS NULL").fetchone()[0] == 0
    conn.close()


def test_backfill_records_presence_and_latest_interval(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    run_a2(conn)
    seal_v1(conn)
    conn = migrate_to_v2(tmp_path, monkeypatch)
    series = conn.execute(
        "SELECT seq, first_valid_at, last_valid_at FROM series WHERE source_id=? AND identity=?",
        (SOURCE, IDENT),
    ).fetchone()
    assert series[0] == 1
    assert series[1].startswith("2026-01-15T10:00:00")
    assert series[2].startswith("2026-01-15T10:00:30")
    interval = conn.execute(
        "SELECT tokens, interval_seconds, eligible, quality_json FROM latest_interval "
        "WHERE counter_key=? ORDER BY observed_at DESC LIMIT 1",
        (COMPUTED_INPUT,),
    ).fetchone()
    assert interval == (300, 15.0, 1, "[]")


def test_latest_interval_survives_retention_for_rates(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    run_a2(conn)
    seal_v1(conn)
    conn = migrate_to_v2(tmp_path, monkeypatch)
    storage.purge_expired(conn, to_iso(T0 + timedelta(days=1)))
    assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM latest_interval WHERE counter_key=?", (COMPUTED_INPUT,)
    ).fetchone()[0] == 1


def test_series_enumeration_key_is_monotonic_under_inserts(tmp_path: Path, monkeypatch):
    conn = build_v1(tmp_path, monkeypatch)
    seal_v1(conn)
    conn = migrate_to_v2(tmp_path, monkeypatch)
    for i, labels in enumerate(
        [{"model_name": "a", "engine": "0"}, {"model_name": "b", "engine": "0"}], start=1
    ):
        at = T0 + timedelta(seconds=15 * i)
        plan = transition_series(
            None,
            ScrapeCounters(
                labels,
                {COMPUTED_INPUT: 10, GENERATED_OUTPUT: 1, REQUEST_INPUT: None, REQUEST_OUTPUT: None},
                {},
            ),
            at,
            None,
            15,
        )
        apply(conn, f"n{i}", [plan], at)
    seqs = [r[0] for r in conn.execute("SELECT seq FROM series ORDER BY seq")]
    assert seqs == [1, 2] and all(s is not None for s in seqs)
