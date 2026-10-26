"""One verified online-snapshot primitive shared by migrations and ``backup``.

Contracts (delivery plan D7, scenario 29): open an existing database without
initializing, migrating or taking the exclusive volume lock; create the
destination exclusively (a pre-existing path — including a symlink — is never
opened or truncated); copy the committed state, WAL included, through SQLite's
online backup API in bounded page steps; verify ``PRAGMA integrity_check`` on
the destination; on any failure remove only the file this call created and
report a stable code. The live main file is never copied and no checkpoint is
used as a substitute.
"""

from __future__ import annotations

import errno
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Pages copied per online-backup step: bounds the work and memory of one step
# and yields the lock between steps so a live writer keeps making progress.
PAGE_STEP = 256
STEP_SLEEP_SECONDS = 0.05

class BackupError(Exception):
    """Stable, payload-free snapshot failure."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class SnapshotInfo:
    source: Path
    destination: Path
    schema_version: int
    page_count: int
    integrity: str


def create_snapshot(source: Path, destination: Path) -> SnapshotInfo:
    """Copy ``source`` into a brand-new database file and verify the result.

    Raises :class:`BackupError` with one of ``source_missing``,
    ``source_not_database``, ``database_corrupt``, ``schema_version_newer``,
    ``output_alias``, ``output_exists``, ``output_unwritable``,
    ``backup_failed`` or ``snapshot_integrity``.
    """
    src = Path(source)
    dst = Path(destination)
    version = _verify_source(src)
    _refuse_alias(src, dst)
    _create_exclusive(dst)
    try:
        info = _copy_and_verify(src, dst, version)
    except BaseException:
        _remove_own_output(dst)
        raise
    return info


def verify_destination(path: Path, *, max_schema_version: int | None = None) -> int:
    """Verify a completed snapshot/backup file; return its schema version.

    Used for migration snapshots and restore inputs: the file must be SQLite,
    carry the dashboard schema, not be newer than this build, and pass
    ``PRAGMA integrity_check``.
    """
    from . import storage

    try:
        return storage.verify_database(path, max_schema_version=max_schema_version)
    except storage.StorageError as exc:
        raise BackupError(exc.code, str(exc)) from exc


def _verify_source(path: Path) -> int:
    from . import storage

    try:
        return storage.verify_database(path)
    except storage.StorageError as exc:
        raise BackupError(exc.code, str(exc)) from exc


def _refuse_alias(src: Path, dst: Path) -> None:
    if os.path.realpath(src) == os.path.realpath(dst):
        raise BackupError(
            "output_alias",
            f"refusing to write the snapshot over its own source ({src})",
        )


def _create_exclusive(dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError as exc:
        raise BackupError("output_exists", f"{dst} already exists; refusing to overwrite") from exc
    except OSError as exc:
        raise BackupError(
            "output_unwritable",
            f"cannot create {dst}: {os.strerror(exc.errno)}"
            if exc.errno in _WRITE_ERRNOS
            else f"cannot create {dst}: {exc}",
        ) from exc
    os.close(fd)


_WRITE_ERRNOS = frozenset(
    {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC, errno.EDQUOT, errno.ENAMETOOLONG}
)


def _copy_and_verify(src: Path, dst: Path, version: int) -> SnapshotInfo:
    src_conn = storage_source_connection(src)
    try:
        dst_conn = sqlite3.connect(dst, isolation_level=None)
        try:
            copy_pages(src_conn, dst_conn)
            check = dst_conn.execute("PRAGMA integrity_check").fetchone()[0]
            pages = int(dst_conn.execute("PRAGMA page_count").fetchone()[0])
        finally:
            # Closing cleanly checkpoints the snapshot's WAL into the single
            # destination file, so the backup is self-contained.
            dst_conn.close()
    except sqlite3.Error as exc:
        raise BackupError("backup_failed", f"online backup failed: {exc}") from exc
    if check != "ok":
        raise BackupError("snapshot_integrity", f"snapshot integrity_check failed: {check}")
    return SnapshotInfo(
        source=src, destination=dst, schema_version=version, page_count=pages, integrity=check
    )


def copy_pages(src_conn: sqlite3.Connection, dst_conn: sqlite3.Connection) -> None:
    """One online-backup copy in bounded page steps (test seam for failures)."""
    src_conn.backup(dst_conn, pages=PAGE_STEP, sleep=STEP_SLEEP_SECONDS)


def storage_source_connection(path: Path) -> sqlite3.Connection:
    """Open the existing source read-write (required for WAL reads) with no DDL.

    The source is never initialized, migrated, or exclusively locked here; the
    online backup API reads a consistent snapshot including committed WAL frames.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA cache_size=-4000")
    conn.execute("PRAGMA mmap_size=0")
    return conn


def _remove_own_output(dst: Path) -> None:
    for candidate in (dst, dst.with_name(dst.name + "-wal"), dst.with_name(dst.name + "-shm")):
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass
        except OSError:
            # Never delete anything we do not own; report the failure instead.
            break


def describe(info: SnapshotInfo) -> str:
    """Payload-free completion line for the CLI (only paths and sizes)."""
    return (
        f"backup complete: {info.destination} (schema {info.schema_version}, "
        f"{info.page_count} pages, integrity {info.integrity})"
    )
