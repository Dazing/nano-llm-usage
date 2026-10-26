"""Offline restore of a verified snapshot into a stopped volume (spec D8).

The restore path is deliberately narrow: the input is validated before the
destination is touched, the previous main/WAL/SHM are preserved together in a
fresh recovery directory, the snapshot is staged on the destination filesystem,
and a durable source-wide ``restore`` event records the recovery. A failure
never leaves the target without its previous database set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import backup, storage
from .backup import BackupError
from .domain import to_iso, utc_now
from .service import os_failure_code, write_failure_code
from .storage import StorageError

log = logging.getLogger("jarvis_dashboard.restore")

RESTORE_EVENT = "restore"
CHUNK = 1 << 16
SIDECAR_SUFFIXES = ("-wal", "-shm")


class RestoreError(Exception):
    """A refusal to restore, carrying a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RestoreInfo:
    input_path: str
    destination: str
    schema_version: int
    page_count: int
    digest: str
    recovery_dir: str | None
    recovered_at: str

    def describe(self) -> str:
        """Operator line with no row data and no credentials."""
        preserved = self.recovery_dir or "none"
        return (
            f"restored {self.page_count} pages (schema {self.schema_version}) from "
            f"{self.input_path} to {self.destination}; previous set: {preserved}; "
            f"digest {self.digest[:12]}"
        )


def _suffixes(path: Path) -> list[str]:
    return list(SIDECAR_SUFFIXES)


def _set_files(db_path: Path) -> list[Path]:
    return [db_path, *[Path(str(db_path) + suffix) for suffix in SIDECAR_SUFFIXES]]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp(now: datetime) -> str:
    return to_iso(now).replace("-", "").replace(":", "").replace(".", "-")


def _recovery_directory(db_path: Path, now: datetime) -> Path:
    """A new directory for the previous set; never reused, never overwritten."""
    base = f"{db_path}.pre-restore-{_stamp(now)}"
    candidate = Path(base)
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = Path(f"{base}-{suffix}")
    candidate.mkdir()
    os.chmod(candidate, 0o750)
    return candidate


def _validate_input(input_path: Path) -> tuple[int, str]:
    if not input_path.is_file():
        raise RestoreError("source_missing", f"snapshot {input_path} does not exist")
    try:
        schema = backup.verify_destination(
            input_path, max_schema_version=storage.CURRENT_SCHEMA_VERSION
        )
    except BackupError as exc:
        raise RestoreError(exc.code, str(exc)) from exc
    return schema, _digest(input_path)


def _stage(input_path: Path, staging: Path) -> None:
    try:
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError as exc:
        raise RestoreError(
            "staging_exists", f"refusing to reuse {staging}; remove it and retry"
        ) from exc
    except OSError as exc:
        raise RestoreError(os_failure_code(exc), str(exc)) from exc
    try:
        with os.fdopen(fd, "wb") as target, input_path.open("rb") as source:
            shutil.copyfileobj(source, target, length=CHUNK)
            target.flush()
            os.fsync(target.fileno())
    except OSError as exc:
        raise RestoreError(os_failure_code(exc), str(exc)) from exc


def _verify_installed(db_path: Path) -> int:
    try:
        schema = backup.verify_destination(
            db_path, max_schema_version=storage.CURRENT_SCHEMA_VERSION
        )
        conn = backup.storage_source_connection(db_path)
        try:
            return int(conn.execute("PRAGMA page_count").fetchone()[0])
        finally:
            conn.close()
    except BackupError as exc:
        raise RestoreError(exc.code, str(exc)) from exc
    except sqlite3.Error as exc:
        raise RestoreError("database_corrupt", str(exc)) from exc


def _record_recovery(db_path: Path, source_id: str, info: RestoreInfo) -> None:
    """Durable source-wide recovery record, written in one transaction."""
    conn = storage.open_storage(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO events (source_id, identity, kind, occurred_at, details_json)"
            " VALUES (?, NULL, ?, ?, ?)",
            (
                source_id,
                RESTORE_EVENT,
                info.recovered_at,
                json.dumps(
                    {
                        "snapshot": info.input_path,
                        "schema_version": info.schema_version,
                        "page_count": info.page_count,
                        "digest": info.digest,
                        "previous_set": info.recovery_dir,
                    },
                    separators=(",", ":"),
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _roll_back(db_path: Path, staging: Path, recovery: Path | None) -> None:
    """Put the preserved set back; the installed copy is kept, never deleted."""
    if staging.exists():
        staging.unlink()
    if recovery is None:
        return
    for path in _set_files(db_path):
        if path.exists():
            os.replace(path, recovery / f"installed-{path.name}")
    original = recovery / db_path.name
    if original.exists():
        os.replace(original, db_path)
    for suffix in SIDECAR_SUFFIXES:
        preserved = recovery / (db_path.name + suffix)
        if preserved.exists():
            os.replace(preserved, Path(str(db_path) + suffix))


def restore_volume(
    db_path: Path | str,
    input_path: Path | str,
    *,
    source_id: str,
    now: datetime | None = None,
) -> RestoreInfo:
    """Replace a stopped volume with ``input_path`` and record the recovery."""
    db_path = Path(db_path)
    input_path = Path(input_path)
    now = now or utc_now()
    schema, digest = _validate_input(input_path)

    try:
        lock = storage.acquire_volume_lock(db_path)
    except StorageError as exc:
        raise RestoreError(exc.code, str(exc)) from exc

    staging = Path(f"{db_path}.restoring")
    recovery: Path | None = None
    try:
        if db_path.exists():
            recovery = _recovery_directory(db_path, now)
            for path in _set_files(db_path):
                if path.exists():
                    os.replace(path, recovery / path.name)

        _stage(input_path, staging)
        os.replace(staging, db_path)
        page_count = _verify_installed(db_path)
        info = RestoreInfo(
            input_path=str(input_path),
            destination=str(db_path),
            schema_version=schema,
            page_count=page_count,
            digest=digest,
            recovery_dir=str(recovery) if recovery else None,
            recovered_at=to_iso(now),
        )
        _record_recovery(db_path, source_id, info)
        return info
    except RestoreError:
        _roll_back(db_path, staging, recovery)
        raise
    except (OSError, sqlite3.Error, StorageError) as exc:
        _roll_back(db_path, staging, recovery)
        code = exc.code if isinstance(exc, StorageError) else (
            os_failure_code(exc) if isinstance(exc, OSError) else write_failure_code(exc)
        )
        raise RestoreError(code, str(exc)) from exc
    except BaseException:
        _roll_back(db_path, staging, recovery)
        raise
    finally:
        lock.release()
