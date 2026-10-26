"""Stopped-volume restore (Delivery 2 Step 6, spec D8, scenarios A29-A30).

Distinguishing scenarios: a live snapshot restores into a fresh stopped volume
with identical accounting state; a running target is refused; a newer or corrupt
snapshot is refused before the destination is touched; the previous
main/WAL/SHM are preserved together and never overwritten; a mid-restore failure
puts the previous set back; collection continues after a restore without
duplicated increments.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from test_storage_helpers import (
    SOURCE,
    T0,
    hourly_totals,
    resume_chain,
    seed_chain,
)

from jarvis_dashboard import backup, restore as restore_mod, storage
from jarvis_dashboard.cli import main
from jarvis_dashboard.restore import RestoreError, restore_volume
from jarvis_dashboard.domain import to_iso

A2_STEPS = [(1000, 100), (1600, 140), (1900, 160)]


def rows(db_path: Path) -> list[tuple]:
    # Read-write open: a stopped volume may need WAL recovery to be read.
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT identity, hour, counter_key, tokens FROM hourly_totals ORDER BY 1, 2, 3"
        ).fetchall()
    finally:
        conn.close()


_open_connections: list = []


def seeded(tmp_path: Path, name: str = "d.sqlite3", model: str = "m") -> Path:
    """A volume written by a still-running collector, so the WAL is live."""
    path = tmp_path / name
    conn = storage.open_storage(path)
    seed_chain(conn, model, A2_STEPS, T0)
    conn.commit()
    _open_connections.append(conn)
    return path


def crash_writer(db_path: Path) -> None:
    """Kill a writer mid-transaction: a real stopped volume keeps WAL and SHM."""
    code = (
        "import time\n"
        "from sqlite3 import connect\n"
        f"c = connect({str(db_path)!r})\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "c.execute(\"INSERT INTO maintenance_cursor(key, value) VALUES('crash', '1')\")\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    proc.stdout.readline()
    proc.kill()
    proc.wait(timeout=10)


def close_writers() -> None:
    for conn in _open_connections:
        conn.close()
    _open_connections.clear()


@pytest.fixture(autouse=True)
def _close_writers():
    yield
    close_writers()


def events(db_path: Path, kind: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT identity, occurred_at, details_json FROM events WHERE kind = ?",
            (kind,),
        ).fetchall()
    finally:
        conn.close()


def test_a_live_snapshot_restores_into_a_fresh_stopped_volume(tmp_path):
    source = seeded(tmp_path)
    assert Path(str(source) + "-wal").exists()  # a live WAL must be included
    snapshot = tmp_path / "snap.sqlite3"
    info = backup.create_snapshot(source, snapshot)
    assert info.integrity == "ok"

    target = tmp_path / "target" / "d.sqlite3"
    target.parent.mkdir()
    restored = restore_volume(target, snapshot, source_id=SOURCE, now=T0)

    assert restored.recovery_dir is None
    assert restored.schema_version == storage.CURRENT_SCHEMA_VERSION
    # The snapshot carried the committed WAL, so the target needs no sidecars
    # (a later read-only open is allowed to create its own).
    assert not Path(str(target) + "-wal").exists()
    assert not Path(str(target) + "-shm").exists()
    assert rows(target) == rows(snapshot)
    # The recovery is durable and source-wide (no series identity, no secrets).
    recorded = events(target, "restore")
    assert len(recorded) == 1
    assert recorded[0][0] is None
    assert recorded[0][1] == to_iso(T0)
    assert restored.digest in recorded[0][2]
    assert str(snapshot) in recorded[0][2]


def test_collection_continues_after_a_restore_without_duplicates(tmp_path):
    source = seeded(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(source, snapshot)

    target = tmp_path / "d.sqlite3"
    restore_volume(target, snapshot, source_id=SOURCE, now=T0)
    conn = storage.open_storage(target)
    before = hourly_totals(conn)
    assert before["computed_input"] == 900
    # Continue the chain from the restored durable state: one new interval only.
    resume_chain(conn, "m", [(2000, 170)], T0 + timedelta(seconds=45))
    after = hourly_totals(conn)
    assert after["computed_input"] == 900 + 100
    assert after["generated_output"] == 60 + 10
    assert after["request_input"] == 2  # the replayed baseline adds nothing
    conn.close()


def test_restore_refuses_a_running_target(tmp_path):
    target = seeded(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(target, snapshot)
    original = target.read_bytes()

    lock = storage.acquire_volume_lock(target)
    try:
        with pytest.raises(RestoreError) as exc:
            restore_volume(target, snapshot, source_id=SOURCE, now=T0)
    finally:
        lock.release()
    assert exc.value.code == "volume_locked"
    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.pre-restore-*"))


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda p: p.write_bytes(b"not a database at all" * 40), "source_not_database"),
        (lambda p: p.write_bytes(b""), "source_missing"),
    ],
)
def test_restore_refuses_an_unusable_snapshot_before_touching_the_target(
    tmp_path, mutate, code
):
    target = seeded(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(target, snapshot)
    original = target.read_bytes()
    mutate(snapshot)

    with pytest.raises(RestoreError) as exc:
        restore_volume(target, snapshot, source_id=SOURCE, now=T0)
    assert exc.value.code == code
    assert target.read_bytes() == original


def test_restore_refuses_a_newer_schema(tmp_path):
    target = seeded(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(target, snapshot)
    conn = sqlite3.connect(snapshot)
    conn.execute(f"PRAGMA user_version = {storage.CURRENT_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    original = target.read_bytes()

    with pytest.raises(RestoreError) as exc:
        restore_volume(target, snapshot, source_id=SOURCE, now=T0)
    assert exc.value.code == "schema_version_newer"
    assert target.read_bytes() == original


def test_restore_preserves_the_previous_set_together(tmp_path):
    target = seeded(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(target, snapshot)
    before = rows(target)
    close_writers()
    # A stopped volume can still leave WAL/SHM behind; they belong to the set.
    crash_writer(target)
    assert (tmp_path / "d.sqlite3-wal").exists()
    assert (tmp_path / "d.sqlite3-shm").exists()

    first = restore_volume(target, snapshot, source_id=SOURCE, now=T0)
    recovery = Path(first.recovery_dir)
    assert (recovery / "d.sqlite3").is_file()
    assert (recovery / "d.sqlite3-wal").is_file()
    assert rows(recovery / "d.sqlite3") == before
    assert rows(target) == rows(snapshot)

    # A second restore at the same instant creates a new directory; the first
    # preserved set stays intact.
    second = restore_volume(target, snapshot, source_id=SOURCE, now=T0)
    assert Path(second.recovery_dir) != recovery
    assert rows(recovery / "d.sqlite3") == before


def test_a_failed_install_puts_the_previous_set_back(tmp_path, monkeypatch):
    target = seeded(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(target, snapshot)
    before = rows(target)
    close_writers()
    crash_writer(target)

    def failing_stage(_input: Path, staging: Path) -> None:
        staging.write_bytes(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore_mod, "_stage", failing_stage)
    with pytest.raises(RestoreError) as exc:
        restore_volume(target, snapshot, source_id=SOURCE, now=T0)
    assert exc.value.code == "storage_full"

    assert rows(target) == before
    assert Path(str(target) + "-wal").exists()  # the preserved sidecar is back
    assert not (tmp_path / "d.sqlite3.restoring").exists()
    recovery = next(tmp_path.glob("*.pre-restore-*"))
    # Nothing was installed, so the recovery directory is emptied back out.
    assert not (recovery / "installed-d.sqlite3").exists()
    assert not (recovery / "d.sqlite3").exists()


def test_a_failed_recovery_record_keeps_the_previous_volume_readable(tmp_path, monkeypatch):
    target = seeded(tmp_path)
    other = seeded(tmp_path, "other.sqlite3", model="snapshot-model")
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(other, snapshot)
    before = rows(target)
    assert rows(snapshot) != before

    def failing_record(_db: Path, _source: str, _info) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(restore_mod, "_record_recovery", failing_record)
    with pytest.raises(RestoreError):
        restore_volume(target, snapshot, source_id=SOURCE, now=T0)
    # The previous set is back in place; the attempted install is kept aside.
    assert rows(target) == before
    recovery = recovery_dir(tmp_path, target)
    assert rows(recovery / "installed-d.sqlite3") == rows(snapshot)


def recovery_dir(tmp_path: Path, target: Path) -> Path:
    return next(tmp_path.glob(f"{target.name}.pre-restore-*"))


def test_restore_cli_reports_success_and_refusals(tmp_path, monkeypatch):
    target = seeded(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(target, snapshot)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("RADIANCE_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("DATABASE_PATH", str(fresh / "d.sqlite3"))

    assert main(["restore", "--input", str(snapshot)]) == 0
    assert rows(fresh / "d.sqlite3") == rows(snapshot)

    assert main(["restore", "--input", str(tmp_path / "absent.sqlite3")]) == 3


def test_restore_needs_no_upstream_configuration(tmp_path, monkeypatch):
    target = seeded(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    backup.create_snapshot(target, snapshot)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("RADIANCE_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("DATABASE_PATH", str(fresh / "d.sqlite3"))
    assert main(["restore", "--input", str(snapshot)]) == 0
    conn = storage.open_storage(fresh / "d.sqlite3")
    assert hourly_totals(conn)["computed_input"] == 900
    conn.close()
