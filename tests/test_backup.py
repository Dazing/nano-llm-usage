"""Delivery 2 Step 1: one verified online snapshot primitive (plan D7, D2-AC5).

Scenarios 29 (backup consistency) and the migration part of 30. The writer
keeps committing to WAL while the snapshot runs; failures must never advertise
an incomplete file as a backup and must never touch a pre-existing file.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from jarvis_dashboard import storage
from jarvis_dashboard import backup as backup_module
from jarvis_dashboard.backup import BackupError, create_snapshot
from jarvis_dashboard.domain import COMPUTED_INPUT, GENERATED_OUTPUT

from test_storage_helpers import T0, apply_seed, hourly_totals

MIGRATIONS_V1 = [(1, (Path(__file__).parent.parent / "src" / "jarvis_dashboard" / "migrations" / "0001_initial.sql").read_text(encoding="utf-8"))]


def read_totals(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return hourly_totals(conn)
    finally:
        conn.close()


def test_backup_of_live_writer_includes_committed_wal_state(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    writer = storage.open_storage(db)
    apply_seed(writer)

    stop = threading.Event()

    def keep_writing() -> None:
        """A live writer committing WAL frames while the snapshot runs."""
        extra = storage.open_storage(db)
        try:
            for i in range(12):
                if stop.is_set():
                    return
                extra.execute(
                    "INSERT INTO collector_state(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"tick{i}", str(i)),
                )
                extra.commit()
                time.sleep(0.01)
        finally:
            extra.close()

    thread = threading.Thread(target=keep_writing, daemon=True)
    thread.start()
    out = tmp_path / "backup.sqlite3"
    info = create_snapshot(db, out)
    stop.set()
    thread.join(timeout=5)

    assert info.integrity == "ok"
    assert info.schema_version == storage.CURRENT_SCHEMA_VERSION
    # Cleanly closed snapshot is self-contained: the committed WAL content is
    # inside the single destination file, not beside it.
    assert not out.with_name(out.name + "-wal").exists()
    assert read_totals(out) == read_totals(db)
    assert COMPUTED_INPUT in read_totals(out)
    assert GENERATED_OUTPUT in read_totals(out)


def test_backup_totals_are_one_committed_snapshot(tmp_path: Path):
    """Scenario 29: the backup matches one committed snapshot, not later live totals."""
    db = tmp_path / "d.sqlite3"
    writer = storage.open_storage(db)
    apply_seed(writer)
    before = read_totals(db)
    out = tmp_path / "b.sqlite3"
    create_snapshot(db, out)

    # Advance the live database *after* the snapshot was completed.
    writer.execute(
        "INSERT INTO hourly_totals(source_id, identity, hour, counter_key, tokens, "
        "observed_intervals, quality_json) VALUES('s1','i','2026-01-15T11Z','x',5,1,'[]')"
    )
    writer.commit()
    assert read_totals(out) == before
    assert "x" not in read_totals(out)


def test_refuses_existing_output_and_leaves_it_untouched(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    storage.open_storage(db).close()
    out = tmp_path / "b.sqlite3"
    out.write_bytes(b"PRECIOUS BACKUP")
    with pytest.raises(BackupError) as exc:
        create_snapshot(db, out)
    assert exc.value.code == "output_exists"
    assert out.read_bytes() == b"PRECIOUS BACKUP"
    assert not out.with_name(out.name + "-wal").exists()


def test_refuses_symlinked_output_and_leaves_target_untouched(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    storage.open_storage(db).close()
    real = tmp_path / "real.txt"
    real.write_bytes(b"KEEP")
    link = tmp_path / "b.sqlite3"
    link.symlink_to(real)
    with pytest.raises(BackupError) as exc:
        create_snapshot(db, link)
    assert exc.value.code == "output_exists"
    assert real.read_bytes() == b"KEEP"


def test_refuses_source_destination_aliasing(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    storage.open_storage(db).close()
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "d.sqlite3").symlink_to(db)
    with pytest.raises(BackupError) as exc:
        create_snapshot(db, nested / "d.sqlite3")
    assert exc.value.code == "output_alias"


def test_unwritable_output_directory_creates_nothing(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    storage.open_storage(db).close()
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    out = ro / "b.sqlite3"
    with pytest.raises(BackupError) as exc:
        create_snapshot(db, out)
    assert exc.value.code == "output_unwritable"
    assert not out.exists()
    ro.chmod(0o700)


@pytest.mark.parametrize("kind", ["missing", "not_sqlite", "foreign_schema"])
def test_invalid_source_is_refused_without_initializing(tmp_path: Path, kind: str):
    if kind == "missing":
        db = tmp_path / "nope.sqlite3"
    elif kind == "not_sqlite":
        db = tmp_path / "junk.sqlite3"
        db.write_bytes(b"hello world, definitely not sqlite")
    else:
        db = tmp_path / "foreign.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE other(x)")
        conn.commit()
        conn.close()
    with pytest.raises(BackupError) as exc:
        create_snapshot(db, tmp_path / "b.sqlite3")
    assert exc.value.code in {"source_missing", "source_not_database"}
    if kind == "missing":
        assert not db.exists()


def test_failed_copy_removes_only_its_own_partial_output(tmp_path: Path, monkeypatch):
    db = tmp_path / "d.sqlite3"
    apply_seed(storage.open_storage(db))
    out = tmp_path / "b.sqlite3"

    def boom(src_conn, dst_conn):  # noqa: ANN001
        src_conn.backup(dst_conn, pages=1, sleep=0.0)  # partial copy lands on disk
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(backup_module, "copy_pages", boom)
    with pytest.raises(BackupError) as exc:
        backup_module.create_snapshot(db, out)
    assert exc.value.code == "backup_failed"
    assert not out.exists()
    assert not out.with_name(out.name + "-wal").exists()


def test_migration_snapshot_is_verified_and_applied(tmp_path: Path, monkeypatch):
    db = tmp_path / "d.sqlite3"
    apply_seed(storage.open_storage(db)).close()
    pending = storage._migration_files() + [(3, "CREATE TABLE extra(x INTEGER);")]
    monkeypatch.setattr(storage, "CURRENT_SCHEMA_VERSION", 3)
    monkeypatch.setattr(storage, "_migration_files", lambda: pending)
    conn = storage.open_storage(db)
    conn.close()
    snap = db.with_name("d.sqlite3.pre-migration-3.sqlite3")
    assert snap.exists()
    # The snapshot captured the pre-migration schema (version 2), verified.
    assert storage.verify_database(snap) == 2


def test_migration_refuses_pre_existing_snapshot_and_applies_nothing(tmp_path: Path, monkeypatch):
    db = tmp_path / "d.sqlite3"
    apply_seed(storage.open_storage(db)).close()
    before = storage.CURRENT_SCHEMA_VERSION
    snap = db.with_name(f"d.sqlite3.pre-migration-{before + 1}.sqlite3")
    snap.write_bytes(b"EXISTING SNAPSHOT")
    pending = storage._migration_files() + [(before + 1, "CREATE TABLE extra(x INTEGER);")]
    monkeypatch.setattr(storage, "CURRENT_SCHEMA_VERSION", before + 1)
    monkeypatch.setattr(storage, "_migration_files", lambda: pending)
    with pytest.raises(storage.StorageError) as exc:
        storage.open_storage(db)
    assert exc.value.code == "snapshot_exists"
    assert snap.read_bytes() == b"EXISTING SNAPSHOT"
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == before
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='extra'"
    ).fetchone()[0] == 0
    conn.close()


def test_backup_needs_no_upstream_and_works_on_fresh_volume(tmp_path: Path):
    """Backup is a storage-only operation (plan D7): no Jarvis access required."""
    db = tmp_path / "data" / "d.sqlite3"
    storage.open_storage(db).close()
    info = create_snapshot(db, tmp_path / "out" / "b.sqlite3")
    assert info.schema_version == storage.CURRENT_SCHEMA_VERSION
    assert info.page_count >= 0
    assert T0.year == 2026  # seed helper sanity: fixture history is in the past
