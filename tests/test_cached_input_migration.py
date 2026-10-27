"""Cost plan Step 2: migration 0003 adds the durable cached-input columns.

Covers acceptance criterion AC7: a version-2 database holding the A2 totals
(900/60/960) migrates to schema 3 behind a verified ``pre-migration-3``
snapshot, keeps every prior total, leaves pre-cache-tracking samples with null
``cached_input`` (never invented), and the writer records the new counter's raw
value, delta and hourly total afterwards.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jarvis_dashboard import storage
from jarvis_dashboard.accounting import transition_series
from jarvis_dashboard.domain import (
    CACHED_INPUT,
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    ScrapeCounters,
    StatsEvidence,
    canonical_labels_json,
)

from test_storage_helpers import downgrade_to_v1

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "jarvis_dashboard" / "migrations"
MIG_0001 = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
MIG_0002 = (MIGRATIONS_DIR / "0002_api_metadata.sql").read_text(encoding="utf-8")

T0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
HOUR = "2026-01-15T10Z"
L = {"model_name": "m", "engine": "0"}
IDENT = canonical_labels_json(L)
SOURCE = "s1"

# Acceptance scenario A1+A2: baseline (1000,100) then +600/+40 and +300/+20.
A2_STEPS = [(1000, 100), (1600, 140), (1900, 160)]
A2_EXPECTED = [(COMPUTED_INPUT, 900), (GENERATED_OUTPUT, 60)]
# Cache tracking only exists from the second interval on (baseline, +300).
A2_CACHED_LATER = [None, 400, 700]


def counters(ci: int, go: int, ca: int | None = None) -> ScrapeCounters:
    return ScrapeCounters(
        dict(L),
        {
            "computed_input": ci,
            "generated_output": go,
            "request_input": None,
            "request_output": None,
            CACHED_INPUT: ca,
        },
        {},
    )


def stats(at: datetime, uptime: float = 1000.0) -> StatsEvidence:
    return StatsEvidence("m", uptime, at, at)


def run_chain(conn, prefix: str, cached: list[int | None] | None = None) -> None:
    """Scenario A1+A2 (900/60 processed 960); ``cached`` adds the cache counter."""
    values = [None, None, None] if cached is None else cached
    state = None
    for i, (ci, go) in enumerate(A2_STEPS):
        at = T0 + timedelta(seconds=15 * i)
        plan = transition_series(
            state, counters(ci, go, values[i]), at, stats(at, 1000 + 15 * i), 15
        )
        storage.apply_poll(
            conn,
            source_id=SOURCE,
            endpoint_fingerprint="fp1",
            poll_id=f"{prefix}{i}",
            observed_at=at,
            plans=[plan],
        )
        state = plan.state


def build_v2(tmp_path: Path, monkeypatch) -> sqlite3.Connection:
    """Write A2 with the current writer, then reduce the database to schema 2.

    ``downgrade_to_v1`` removes the 0002 and 0003 additions; reopening through
    the pinned Delivery 2 migration list restores exactly schema 2, which is the
    state this migration must upgrade.
    """
    db = tmp_path / "d.sqlite3"
    conn = storage.open_storage(db)
    run_chain(conn, "v1")
    downgrade_to_v1(conn)
    conn.close()
    with monkeypatch.context() as step:
        step.setattr(storage, "CURRENT_SCHEMA_VERSION", 2)
        step.setattr(storage, "_migration_files", lambda: [(1, MIG_0001), (2, MIG_0002)])
        # The Delivery 2 image had no cached-input column to scan in its backfill.
        step.setattr(storage, "run_backfills", lambda conn, **_kw: 0)
        conn = storage.open_storage(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    columns = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
    assert CACHED_INPUT not in columns
    return conn


def migrate_to_v3(tmp_path: Path) -> sqlite3.Connection:
    """Upgrade the version-2 database through the real migration path."""
    return storage.open_storage(tmp_path / "d.sqlite3")


def totals(conn) -> list[tuple]:
    return conn.execute(
        "SELECT counter_key, sum(tokens) FROM hourly_totals WHERE tokens IS NOT NULL "
        "GROUP BY counter_key ORDER BY 1"
    ).fetchall()


def hourly_row(conn, counter=CACHED_INPUT, hour=HOUR):
    return conn.execute(
        "SELECT tokens, observed_intervals FROM hourly_totals "
        "WHERE source_id=? AND identity=? AND hour=? AND counter_key=?",
        (SOURCE, IDENT, hour, counter),
    ).fetchone()


def sample_rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT cached_input, delta_cached_input FROM samples "
        "WHERE source_id=? AND identity=? ORDER BY sample_id",
        (SOURCE, IDENT),
    ).fetchall()


def test_v2_database_migrates_to_v3_with_snapshot(tmp_path: Path, monkeypatch):
    conn = build_v2(tmp_path, monkeypatch)
    conn.close()

    conn = migrate_to_v3(tmp_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == storage.CURRENT_SCHEMA_VERSION == 3
    snapshot = tmp_path / "d.sqlite3.pre-migration-3.sqlite3"
    assert snapshot.exists()
    # The snapshot captured the pre-migration (version 2) schema, verified.
    assert storage.verify_database(snapshot) == 2
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # AC7: the migrated database still answers the A2 totals (900/60, 960 total).
    assert totals(conn) == A2_EXPECTED
    columns = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
    assert {CACHED_INPUT, "delta_cached_input"} <= columns
    # hourly_totals is keyed by counter, so no new column is needed there.
    assert "cached_input" not in {
        row[1] for row in conn.execute("PRAGMA table_info(hourly_totals)")
    }
    conn.close()


def test_pre_cache_samples_stay_null(tmp_path: Path, monkeypatch):
    conn = build_v2(tmp_path, monkeypatch)
    before = conn.execute("SELECT count(*) FROM samples").fetchone()[0]
    assert before > 0
    conn.close()

    conn = migrate_to_v3(tmp_path)
    rows = sample_rows(conn)
    assert len(rows) == before
    assert rows == [(None, None)] * before  # unobserved, never zero-filled
    assert hourly_row(conn) is None  # an unobserved counter is absent, never zero
    conn.close()


def test_migration_is_idempotent(tmp_path: Path, monkeypatch):
    conn = build_v2(tmp_path, monkeypatch)
    conn.close()
    migrate_to_v3(tmp_path).close()
    snapshot = tmp_path / "d.sqlite3.pre-migration-3.sqlite3"
    before = snapshot.stat().st_mtime_ns

    conn = migrate_to_v3(tmp_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert snapshot.stat().st_mtime_ns == before  # the snapshot is never rewritten
    assert totals(conn) == A2_EXPECTED
    conn.close()


def test_writer_records_cached_input_after_migration(tmp_path: Path, monkeypatch):
    conn = build_v2(tmp_path, monkeypatch)
    conn.close()

    conn = migrate_to_v3(tmp_path)
    before = len(sample_rows(conn))
    run_chain(conn, "v3", cached=A2_CACHED_LATER)

    # The pre-migration rows keep their nulls; the new chain records raw + delta,
    # and the first sight of the counter is a baseline with no delta.
    assert sample_rows(conn)[before:] == [(400, None), (700, 300)]
    assert all(row == (None, None) for row in sample_rows(conn)[:before])
    assert hourly_row(conn) == (300, 1)
    assert totals(conn) == [
        (CACHED_INPUT, 300),
        (COMPUTED_INPUT, 1800),
        (GENERATED_OUTPUT, 120),
    ]
    conn.close()
