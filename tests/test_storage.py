from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from test_storage_helpers import downgrade_to_v1

from jarvis_dashboard import storage
from jarvis_dashboard.accounting import transition_series
from jarvis_dashboard.domain import (
    EpochReason,
    INT64_MAX,
    ScrapeCounters,
    StatsEvidence,
    canonical_labels_json,
)

T0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
L = {"model_name": "m", "engine": "0"}
IDENT = canonical_labels_json(L)


def counters(ci, go):
    return ScrapeCounters(
        dict(L),
        {"computed_input": ci, "generated_output": go, "request_input": None, "request_output": None},
        {},
    )


def stats(at: datetime, uptime: float):
    return StatsEvidence("m", uptime, at, at)


def plan1():
    return transition_series(None, counters(100, 10), T0, stats(T0, 100), 15)


def seed(conn):
    apply(conn, "p1", [plan1()])
    apply(conn, "p2", [plan2()], T0 + timedelta(seconds=180))


def plan2():
    return transition_series(plan1().state, counters(400, 40), T0 + timedelta(seconds=180), stats(T0 + timedelta(seconds=180), 280), 15)


def apply(conn, poll_id, plans, at=T0):
    return storage.apply_poll(
        conn, source_id="s1", endpoint_fingerprint="fp1", poll_id=poll_id, observed_at=at, plans=plans
    )


def test_fresh_database_gets_schema(tmp_path: Path):
    conn = storage.open_storage(tmp_path / "d.sqlite3")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == storage.CURRENT_SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sources", "series", "series_state", "epochs", "samples", "hourly_totals",
            "gap_spans", "events", "collector_state", "maintenance_cursor"} <= tables


def test_replayed_poll_id_applies_nothing(tmp_path: Path):
    conn = storage.open_storage(tmp_path / "d.sqlite3")
    apply(conn, "p1", [plan1()])
    apply(conn, "p2", [plan2()], T0 + timedelta(seconds=180))
    before = conn.execute("SELECT hour, tokens FROM hourly_totals ORDER BY hour, tokens").fetchall()
    assert storage.apply_poll(conn, source_id="s1", endpoint_fingerprint="fp1", poll_id="p2",
                              observed_at=T0 + timedelta(seconds=180), plans=[plan2()]) == 0
    assert conn.execute("SELECT hour, tokens FROM hourly_totals ORDER BY hour, tokens").fetchall() == before
    # scenario D26: repeated identical value under a NEW poll id -> zero delta, not a duplicate
    p3 = transition_series(plan2().state, counters(400, 40), T0 + timedelta(seconds=195), stats(T0 + timedelta(seconds=195), 295), 15)
    assert apply(conn, "p3", [p3]) == 1
    row = conn.execute(
        "SELECT delta_computed_input, computed_input FROM samples ORDER BY sample_id DESC LIMIT 1"
    ).fetchone()
    assert row == (0, 400)


def test_crash_before_commit_leaves_no_state(tmp_path: Path):
    conn = storage.open_storage(tmp_path / "d.sqlite3")
    seed(conn)
    conn.execute(
        "UPDATE hourly_totals SET tokens=? WHERE hour='2026-01-15T10Z' AND counter_key='computed_input'",
        (INT64_MAX - 50,),
    )
    plan3 = transition_series(plan2().state, counters(700, 70), T0 + timedelta(seconds=195), stats(T0 + timedelta(seconds=195), 295), 15)
    with pytest.raises(storage.StorageError) as exc:
        apply(conn, "p99", [plan3], T0 + timedelta(seconds=195))
    assert exc.value.code == "hourly_overflow"
    # baselines and samples never advanced (D24/C22)
    st = storage.load_series(conn, "s1")[IDENT].state
    assert st.primary.computed_input == 400  # baseline from p1, not advanced by p2
    assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 1
    assert conn.execute(
        "SELECT tokens FROM hourly_totals WHERE hour='2026-01-15T10Z' AND counter_key='computed_input'"
    ).fetchone()[0] == INT64_MAX - 50


def test_retention_keeps_hourly_gaps_and_epochs(tmp_path: Path):
    conn = storage.open_storage(tmp_path / "d.sqlite3")
    seed(conn)
    cutoff = (T0 + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    removed = storage.purge_expired(conn, cutoff)
    assert removed >= 1
    assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM hourly_totals").fetchone()[0] > 0
    assert conn.execute("SELECT count(*) FROM epochs").fetchone()[0] > 0
    assert conn.execute("SELECT count(*) FROM gap_spans").fetchone()[0] > 0
    assert conn.execute("SELECT count(*) FROM series_state").fetchone()[0] == 1
    assert storage.get_runtime_state(conn, "retention_cutoff") is None  # cursor table, not state
    assert conn.execute("SELECT value FROM maintenance_cursor WHERE key='retention_cutoff'").fetchone()[0] == cutoff


def test_newer_schema_is_refused(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    conn = storage.open_storage(db)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(storage.StorageError) as exc:
        storage.open_storage(db)
    assert exc.value.code == "schema_version_newer"


def test_corrupt_database_is_refused_not_replaced(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    conn = storage.open_storage(db)
    seed(conn)
    conn.close()
    data = bytearray(db.read_bytes())
    # Truncation is unambiguous corruption: the declared page count no longer
    # matches the file, so integrity_check must fail rather than be repaired.
    db.write_bytes(bytes(data[: len(data) // 2]))
    with pytest.raises(storage.StorageError) as exc:
        storage.open_storage(db)
    assert exc.value.code in {"database_corrupt", "database_not_sqlite"}


def test_foreign_file_is_refused(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    db.write_bytes(b"not a database at all")
    with pytest.raises(storage.StorageError) as exc:
        storage.open_storage(db)
    assert exc.value.code == "database_not_sqlite"


def _v1_database(tmp_path: Path, monkeypatch, db: Path) -> None:
    """Create a database exactly as Delivery 1 shipped it (schema version 1)."""
    conn = storage.open_storage(db)
    seed(conn)
    downgrade_to_v1(conn)
    conn.close()


def test_migration_snapshots_and_preserves_rows(tmp_path: Path, monkeypatch):
    db = tmp_path / "d.sqlite3"
    _v1_database(tmp_path, monkeypatch, db)
    original_sql = storage._migration_files()[0][1]
    monkeypatch.setattr(storage, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        storage, "_migration_files", lambda: [(1, original_sql), (2, "ALTER TABLE sources ADD COLUMN note TEXT")]
    )
    conn = storage.open_storage(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 1
    snapshot = Path(f"{db}.pre-migration-2.sqlite3")
    assert snapshot.exists()
    # The snapshot is a verified copy, not a file that merely exists.
    assert storage.verify_database(snapshot) == 1
    conn.close()


def test_failed_migration_leaves_database_intact(tmp_path: Path, monkeypatch):
    db = tmp_path / "d.sqlite3"
    _v1_database(tmp_path, monkeypatch, db)
    original_sql = storage._migration_files()[0][1]
    monkeypatch.setattr(storage, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        storage, "_migration_files", lambda: [(1, original_sql), (2, "CREATE TABLE broken(x CONSTRAINT y;")]
    )
    with pytest.raises(storage.StorageError) as exc:
        storage.open_storage(db)
    assert exc.value.code == "migration_failed"
    conn = storage._connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 1


def test_second_volume_lock_is_refused(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    storage.open_storage(db)
    lock = storage.acquire_volume_lock(db)
    code = (
        "import sys; sys.path.insert(0,'src'); from pathlib import Path;"
        "from jarvis_dashboard import storage;"
        f"import json;\n"
        "try:\n"
        f"    storage.acquire_volume_lock(Path({str(db)!r}))\n"
        "    print('ACQUIRED')\n"
        "except storage.StorageError as e:\n"
        f"    print(e.code)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(Path(__file__).parent.parent))
    assert out.stdout.strip() == "volume_locked"
    lock.release()
    storage.acquire_volume_lock(db).release()


def test_endpoint_change_preserves_history(tmp_path: Path):
    conn = storage.open_storage(tmp_path / "d.sqlite3")
    seed(conn)
    before = conn.execute("SELECT sum(tokens) FROM hourly_totals").fetchone()[0]
    hinted = transition_series(
        plan2().state, counters(5000, 500), T0 + timedelta(seconds=195), stats(T0 + timedelta(seconds=195), 295), 15,
        new_epoch_hint=EpochReason.ENDPOINT_CHANGE,
    )
    assert hinted.new_epoch is not None and hinted.new_epoch.reason is EpochReason.ENDPOINT_CHANGE
    apply(conn, "p9", [hinted], T0 + timedelta(seconds=195))
    reasons = [r[0] for r in conn.execute("SELECT reason FROM epochs ORDER BY epoch_id").fetchall()]
    assert reasons.count("new_series") == 1 and reasons[-1] == "endpoint_change"
    assert conn.execute("SELECT sum(tokens) FROM hourly_totals").fetchone()[0] == before  # no credit on epoch
    assert storage.stored_endpoint_fingerprint(conn, "s1") == "fp1"
    storage.record_source_attempt(conn, "s1", "fp2", T0 + timedelta(seconds=30), None)
    assert storage.stored_endpoint_fingerprint(conn, "s1") == "fp2"
