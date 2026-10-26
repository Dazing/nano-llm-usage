"""Durable SQLite storage: volume lock, schema migration, atomic poll application.

All mutations are single-writer behind an exclusive lock file. A poll is
applied in one transaction so baselines, samples, hourly totals, gaps and
events commit together (all-or-none).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from . import backup
from .accounting import HourDelta, HourGap, SeriesPlan
from .domain import (
    COUNTERS_FOR_SCOPE,
    COUNTER_KEYS,
    PRIMARY_KEYS,
    OpenGap,
    OptionalSlot,
    PrimarySlot,
    QualityFlag,
    SeriesState,
    canonical_labels_json,
    checked_add_int64,
    merge_quality,
    parse_iso,
    to_iso,
)

CURRENT_SCHEMA_VERSION = 2
# Max hourly gap rows materialized per transaction (resumable beyond that).
GAP_MATERIALIZE_BATCH = 500
GAP_QUEUE_KEY = "gap_materialization_queue"
LAST_RETENTION_KEY = "last_retention_at"
# Resumable cursor for the 0002 legacy coverage backfill (cleared when done).
API_METADATA_BACKFILL_KEY = "api_metadata_backfill"
BACKFILL_BATCH = 400
_SQLITE_HEADER = b"SQLite format 3\0"

RUNTIME_STORAGE_STATUS = "storage_status"
RUNTIME_COLLECTOR_STATUS = "collector_status"
RUNTIME_LAST_POLL_AT = "last_poll_at"
RUNTIME_LAST_ERROR_CODE = "last_error_code"
RUNTIME_PRIMARY_SUCCESS_AT = "last_primary_success_at"


class StorageError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class LoadedSeries:
    """Persisted series state plus its epoch id, ready for the next poll."""

    identity: str
    labels: dict[str, str]
    state: SeriesState
    epoch_id: int


@dataclass(frozen=True)
class VolumeLock:
    path: Path
    _fh: object  # kept open for the lock's lifetime

    def release(self) -> None:
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()


def acquire_volume_lock(db_path: Path) -> VolumeLock:
    """Exclusive lock guarding the database volume (D6/D27)."""
    lock_path = db_path.with_name(db_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        raise StorageError(
            "volume_locked",
            f"another collector holds the volume lock on {lock_path}",
        ) from exc
    return VolumeLock(path=lock_path, _fh=fh)


WRITER_CACHE_KIB = 4000  # 4 MiB initial page cache (specification D5)
READER_CACHE_KIB = 4000
READER_BUSY_TIMEOUT_MS = 2000


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(f"PRAGMA cache_size=-{WRITER_CACHE_KIB}")
    conn.execute("PRAGMA mmap_size=0")  # no unbounded mmap (specification D5)
    return conn


def ro_uri(db_path: Path) -> str:
    """Read-only SQLite URI for ``db_path`` (escaping reserved URI characters)."""
    return f"file:{quote(str(Path(db_path).resolve()), safe='/:')}?mode=ro"


@contextlib.contextmanager
def read_snapshot(db_path: Path) -> Iterator[sqlite3.Connection]:
    """One bounded read-only snapshot connection for one API response.

    ``mode=ro`` (never ``immutable=1``, which is unsafe with a live WAL),
    ``query_only``, a bounded busy timeout, a 4 MiB cache, no mmap, and one
    deferred read transaction covering the whole response so every query sees
    the same committed snapshot. The connection is always closed.
    """
    conn = sqlite3.connect(ro_uri(db_path), uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={READER_BUSY_TIMEOUT_MS}")
        conn.execute(f"PRAGMA cache_size=-{READER_CACHE_KIB}")
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def verify_database(path: Path, *, max_schema_version: int | None = None) -> int:
    """Validate an existing dashboard database read-only; return its schema version.

    Refuses missing/empty files, non-SQLite files, SQLite files without the
    dashboard schema, databases failing ``integrity_check`` and schemas newer
    than this build. Never creates, migrates or locks anything.
    """
    path = Path(path)
    limit = CURRENT_SCHEMA_VERSION if max_schema_version is None else max_schema_version
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise StorageError("source_missing", f"{path} is not readable: {exc}") from exc
    if size == 0:
        raise StorageError("source_missing", f"{path} is empty")
    with path.open("rb") as fh:
        header = fh.read(len(_SQLITE_HEADER))
    if header != _SQLITE_HEADER:
        raise StorageError("source_not_database", f"{path} is not a SQLite database")
    try:
        conn = sqlite3.connect(ro_uri(path), uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise StorageError("database_corrupt", f"cannot open {path}: {exc}") from exc
    try:
        conn.execute(f"PRAGMA busy_timeout={READER_BUSY_TIMEOUT_MS}")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sources'"
            ).fetchone()[0]
            if tables == 0:
                raise StorageError(
                    "source_not_database",
                    f"{path} is a SQLite database without the dashboard schema",
                )
        if version > limit:
            raise StorageError(
                "schema_version_newer",
                f"{path} schema version {version} is newer than this build (max {limit})",
            )
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise StorageError("database_corrupt", f"integrity_check failed: {check}")
    finally:
        conn.close()
    return version


def _migration_files() -> list[tuple[int, str]]:
    root = files("jarvis_dashboard").joinpath("migrations")
    out: list[tuple[int, str]] = []
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        name = entry.name
        if name.endswith(".sql"):
            out.append((int(name.split("_", 1)[0]), entry.read_text(encoding="utf-8")))
    return out


def open_storage(db_path: Path) -> sqlite3.Connection:
    """Open (and, if needed, initialize/migrate) the database.

    Refuses: non-SQLite files, corrupt databases, schemas newer than this
    build, and failed migrations -- never replaces a refused database with a
    fresh one. Snapshots to ``<db>.pre-migration-<version>.sqlite3`` before
    migrating an existing schema.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existing = db_path.exists() and db_path.stat().st_size > 0
    if existing:
        with db_path.open("rb") as fh:
            header = fh.read(len(_SQLITE_HEADER))
        if header != _SQLITE_HEADER:
            raise StorageError("database_not_sqlite", f"{db_path} is not a SQLite database")
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        raise StorageError("database_corrupt", f"cannot open {db_path}: {exc}") from exc
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > CURRENT_SCHEMA_VERSION:
        conn.close()
        raise StorageError(
            "schema_version_newer",
            f"database schema version {version} is newer than this collector "
            f"(supports up to {CURRENT_SCHEMA_VERSION})",
        )
    if existing:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            conn.close()
            raise StorageError("database_corrupt", f"integrity_check failed: {check}")
    migrations = _migration_files()
    if version == 0 and existing:
        # A valid SQLite file without our schema: not a database we own.
        tables = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sources'"
        ).fetchone()[0]
        if tables == 0:
            conn.close()
            raise StorageError(
                "database_not_mine",
                f"{db_path} is a SQLite database without the dashboard schema",
            )
    for number, sql in migrations:
        if number <= version:
            continue
        if version > 0 and existing:
            # A database created by this open has nothing to preserve; only a
            # pre-existing volume needs a pre-migration snapshot.
            _pre_migration_snapshot(db_path, number)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in sql.split(";"):
                body = statement.strip()
                if body and not body.lower().startswith("pragma"):
                    conn.execute(body)
            conn.execute(f"PRAGMA user_version={number}")
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            conn.close()
            raise StorageError("migration_failed", f"migration {number:04d}: {exc}") from exc
        version = number
    if not existing:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            conn.close()
            raise StorageError("database_corrupt", f"fresh database failed integrity_check")
    # A schema at 0002 needs the coverage backfill before the API can answer
    # from materialized metadata; bounded batches resume after an interrupted
    # startup. Delivery 1 schemas have no metadata columns to fill.
    if _has_api_metadata_schema(conn):
        run_backfills(conn)
    return conn


def pre_migration_snapshot_path(db_path: Path, number: int) -> Path:
    return db_path.with_name(f"{db_path.name}.pre-migration-{number}.sqlite3")


def _has_api_metadata_schema(conn: sqlite3.Connection) -> bool:
    """True when the 0002 coverage columns exist (Delivery 1 databases do not)."""
    if "seq" not in {row[1] for row in conn.execute("PRAGMA table_info(series)")}:
        return False
    return "last_success_at" in {row[1] for row in conn.execute("PRAGMA table_info(sources)")}


def _pre_migration_snapshot(db_path: Path, number: int) -> backup.SnapshotInfo:
    """Verified, non-overwriting snapshot taken before a schema change is applied.

    Uses the same online-backup primitive as ``jarvis-dashboard backup``: the
    migration is refused (never applied) when the snapshot cannot be created,
    already exists, or fails ``integrity_check``.
    """
    dest = pre_migration_snapshot_path(db_path, number)
    try:
        return backup.create_snapshot(db_path, dest)
    except backup.BackupError as exc:
        code = "snapshot_exists" if exc.code == "output_exists" else exc.code
        raise StorageError(code, f"pre-migration {number:04d} snapshot refused: {exc}") from exc


def set_runtime_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO collector_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_runtime_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM collector_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def record_source_attempt(
    conn: sqlite3.Connection,
    source_id: str,
    endpoint_fingerprint: str,
    at: datetime,
    error_code: str | None,
    *,
    primary_success: bool = False,
) -> None:
    """Record one collection attempt.

    ``last_seen_at`` tracks attempts; the success timestamps track only polls
    where the primary counters were actually parsed and accepted, so a
    transport success carrying empty/invalid primary data never refreshes
    collection recency (Delivery 2 plan Step 3).
    """
    at_iso = to_iso(at)
    conn.execute(
        "INSERT INTO sources(source_id, endpoint_fingerprint, first_seen_at, last_seen_at, "
        "last_attempt_at, last_error_code, first_success_at, last_success_at) "
        "VALUES(?,?,?,?,?,?,?,?) "
        # The newest attempt wins: the reported error always describes the
        # latest attempt, and success recency only moves forward.
        "ON CONFLICT(source_id) DO UPDATE SET endpoint_fingerprint=excluded.endpoint_fingerprint, "
        "last_seen_at=MAX(excluded.last_seen_at, sources.last_seen_at), "
        "last_attempt_at=CASE WHEN excluded.last_attempt_at >= sources.last_attempt_at "
        "THEN excluded.last_attempt_at ELSE sources.last_attempt_at END, "
        "last_error_code=CASE WHEN excluded.last_attempt_at >= sources.last_attempt_at "
        "THEN excluded.last_error_code ELSE sources.last_error_code END, "
        "first_success_at=COALESCE(sources.first_success_at, excluded.first_success_at), "
        "last_success_at=NULLIF("
        "MAX(COALESCE(excluded.last_success_at, ''), COALESCE(sources.last_success_at, '')), '')",
        (
            source_id,
            endpoint_fingerprint,
            at_iso,
            at_iso,
            at_iso,
            error_code,
            at_iso if primary_success else None,
            at_iso if primary_success else None,
        ),
    )


def source_activity(conn: sqlite3.Connection, source_id: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (first_success_at, last_success_at, last_attempt_at, last_error_code)."""
    row = conn.execute(
        "SELECT first_success_at, last_success_at, last_attempt_at, last_error_code "
        "FROM sources WHERE source_id=?",
        (source_id,),
    ).fetchone()
    return row if row else (None, None, None, None)


def stored_endpoint_fingerprint(conn: sqlite3.Connection, source_id: str) -> str | None:
    row = conn.execute(
        "SELECT endpoint_fingerprint FROM sources WHERE source_id=?", (source_id,)
    ).fetchone()
    return row[0] if row else None


def load_series(conn: sqlite3.Connection, source_id: str) -> dict[str, LoadedSeries]:
    out: dict[str, LoadedSeries] = {}
    rows = conn.execute(
        "SELECT s.identity, s.labels_json, st.epoch_id, st.primary_computed_input, "
        "st.primary_generated_output, st.primary_observed_at, st.continuity_verified, "
        "st.uptime_lo, st.uptime_hi, st.optional_json, st.open_gaps_json, "
        "st.last_accepted_at, e.started_at, st.stale_since "
        "FROM series s JOIN series_state st ON st.source_id=s.source_id AND st.identity=s.identity "
        "JOIN epochs e ON e.epoch_id=st.epoch_id WHERE s.source_id=?",
        (source_id,),
    ).fetchall()
    for r in rows:
        state = SeriesState(
            epoch_id=int(r[2]),
            epoch_started_at=parse_iso(r[12]),
            primary=PrimarySlot(
                computed_input=r[3],
                generated_output=r[4],
                observed_at=None if r[5] is None else parse_iso(r[5]),
                continuity_verified=bool(r[6]),
                uptime_lo=None if r[7] is None else parse_iso(r[7]),
                uptime_hi=None if r[8] is None else parse_iso(r[8]),
            ),
            optional={
                k: OptionalSlot(
                    v["baseline"],
                    None if v["observed_at"] is None else parse_iso(v["observed_at"]),
                    bool(v["continuity_verified"]),
                )
                for k, v in json.loads(r[9]).items()
            },
            open_gaps={
                k: OpenGap(k, parse_iso(v["started_at"]))
                for k, v in json.loads(r[10]).items()
            },
            last_accepted_at=None if r[11] is None else parse_iso(r[11]),
            stale_since=None if r[13] is None else parse_iso(r[13]),
        )
        out[r[0]] = LoadedSeries(
            identity=r[0], labels=json.loads(r[1]), state=state, epoch_id=int(r[2])
        )
    return out


def _dt(value: str) -> datetime:
    from .domain import parse_iso

    return parse_iso(value)


def apply_poll(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    endpoint_fingerprint: str,
    poll_id: str,
    observed_at: datetime,
    plans: list[SeriesPlan],
    primary_success: bool | None = None,
) -> int:
    """Atomically apply one poll's plans. Returns number of series applied.

    Idempotent per ``poll_id``: a replayed poll applies nothing (D25).
    Any overflow/integrity failure rolls the whole poll back so no baseline
    advances (D24/C22/D28).

    ``primary_success`` records collection recency; by default it is derived
    from the plans (at least one series with a usable primary counter pair).
    """
    applied = 0
    gap_pieces: list[tuple[str, HourGap]] = []
    if primary_success is None:
        # A baseline poll observes a usable primary pair as well: presence is
        # judged on the counters, not on whether a sample row was written.
        primary_success = any(_primary_pair_observed_at(plan) is not None for plan in plans)
    conn.execute("BEGIN IMMEDIATE")
    try:
        at_iso = to_iso(observed_at)
        record_source_attempt(
            conn, source_id, endpoint_fingerprint, observed_at, None, primary_success=primary_success
        )
        for plan in plans:
            identity = canonical_labels_json(plan.labels)
            if conn.execute(
                "SELECT 1 FROM samples WHERE source_id=? AND identity=? AND applied_key=?",
                (source_id, identity, poll_id),
            ).fetchone():
                continue
            at_iso_s = at_iso
            if plan.is_new:
                seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM series").fetchone()[0]
                conn.execute(
                    "INSERT INTO series(source_id, identity, labels_json, first_seen_at, "
                    "last_seen_at, stale_since, seq) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_id, identity) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (
                        source_id,
                        identity,
                        json.dumps(plan.labels, sort_keys=True, ensure_ascii=False),
                        at_iso_s,
                        at_iso_s,
                        _opt_iso(plan.state.stale_since),
                        seq,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE series SET last_seen_at=?, stale_since=? WHERE source_id=? AND identity=?",
                    (at_iso_s, _opt_iso(plan.state.stale_since), source_id, identity),
                )
            new_epoch_id: int | None = None
            if plan.new_epoch is not None:
                ne = plan.new_epoch
                cur = conn.execute(
                    "INSERT INTO epochs(source_id, identity, reason, started_at, "
                    "opening_computed_input, opening_generated_output) VALUES(?,?,?,?,?,?)",
                    (
                        source_id,
                        identity,
                        ne.reason.value,
                        to_iso(ne.started_at),
                        ne.opening_computed_input,
                        ne.opening_generated_output,
                    ),
                )
                new_epoch_id = int(cur.lastrowid)
            s = plan.state
            epoch_fk = new_epoch_id
            if epoch_fk is None:
                row = conn.execute(
                    "SELECT epoch_id FROM series_state WHERE source_id=? AND identity=?",
                    (source_id, identity),
                ).fetchone()
                epoch_fk = row[0] if row else s.epoch_id
            conn.execute(
                "INSERT INTO series_state(source_id, identity, epoch_id, primary_computed_input, "
                "primary_generated_output, primary_observed_at, continuity_verified, uptime_lo, "
                "uptime_hi, optional_json, open_gaps_json, last_accepted_at, stale_since) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id, identity) DO UPDATE SET "
                "primary_computed_input=excluded.primary_computed_input, "
                "primary_generated_output=excluded.primary_generated_output, "
                "primary_observed_at=excluded.primary_observed_at, "
                "continuity_verified=excluded.continuity_verified, uptime_lo=excluded.uptime_lo, "
                "uptime_hi=excluded.uptime_hi, optional_json=excluded.optional_json, "
                "open_gaps_json=excluded.open_gaps_json, last_accepted_at=excluded.last_accepted_at, "
                "stale_since=excluded.stale_since",
                (
                    source_id,
                    identity,
                    epoch_fk,
                    s.primary.computed_input,
                    s.primary.generated_output,
                    _opt_iso(s.primary.observed_at),
                    int(s.primary.continuity_verified),
                    _opt_iso(s.primary.uptime_lo),
                    _opt_iso(s.primary.uptime_hi),
                    json.dumps(
                        {
                            k: {
                                "baseline": v.baseline,
                                "observed_at": _opt_iso(v.observed_at),
                                "continuity_verified": v.continuity_verified,
                            }
                            for k, v in s.optional.items()
                        },
                        sort_keys=True,
                    ),
                    json.dumps(
                        {k: {"started_at": _opt_iso(v.started_at)} for k, v in s.open_gaps.items()},
                        sort_keys=True,
                    ),
                    _opt_iso(s.last_accepted_at),
                    _opt_iso(s.stale_since),
                ),
            )
            if new_epoch_id is not None:
                conn.execute(
                    "UPDATE series_state SET epoch_id=? WHERE source_id=? AND identity=?",
                    (new_epoch_id, source_id, identity),
                )
            sample = plan.sample
            if sample is not None:
                conn.execute(
                    "INSERT INTO samples(source_id, identity, applied_key, observed_at, "
                    "computed_input, generated_output, request_input, request_output, "
                    "delta_computed_input, delta_generated_output, delta_request_input, "
                    "delta_request_output, quality_json, rate_eligible) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        identity,
                        poll_id,
                        to_iso(sample.observed_at),
                        sample.counters.get("computed_input"),
                        sample.counters.get("generated_output"),
                        sample.counters.get("request_input"),
                        sample.counters.get("request_output"),
                        sample.deltas.get("computed_input"),
                        sample.deltas.get("generated_output"),
                        sample.deltas.get("request_input"),
                        sample.deltas.get("request_output"),
                        json.dumps(sorted(sample.quality), ensure_ascii=False),
                        int(sample.rate_eligible),
                    ),
                )
            for hd in plan.hour_deltas:
                _apply_hour_delta(conn, source_id, identity, hd)
                _apply_latest_interval(conn, source_id, identity, hd)
            if _primary_pair_observed_at(plan) is not None:
                # Presence of a valid primary pair (series list first/last valid).
                # The baseline observation counts: it proves the engine was seen.
                observed = _primary_pair_observed_at(plan)
                conn.execute(
                    "UPDATE series SET first_valid_at=COALESCE(first_valid_at,?), last_valid_at=? "
                    "WHERE source_id=? AND identity=?",
                    (to_iso(observed), to_iso(observed), source_id, identity),
                )
            if plan.new_epoch is not None:
                # Baseline/reset/restart: no usable preceding interval, so rates
                # stay null until a new accepted interval is recorded (D4).
                conn.execute(
                    "DELETE FROM latest_interval WHERE source_id=? AND identity=? AND counter_key IN (?,?)",
                    (source_id, identity, *PRIMARY_KEYS),
                )
            for hg in plan.hour_gaps:
                gap_pieces.append((identity, hg))
            for closed in plan.gaps_closed:
                cur = conn.execute(
                    "UPDATE gap_spans SET ended_at=? WHERE source_id=? AND identity=? AND scope=? "
                    "AND ended_at IS NULL",
                    (to_iso(closed.ended_at), source_id, identity, closed.scope),
                )
                if cur.rowcount == 0:
                    # Span opened and closed inside this same poll.
                    conn.execute(
                        "INSERT INTO gap_spans(source_id, identity, scope, started_at, ended_at) "
                        "VALUES(?,?,?,?,?)",
                        (
                            source_id,
                            identity,
                            closed.scope,
                            to_iso(closed.started_at),
                            to_iso(closed.ended_at),
                        ),
                    )
            for opened in plan.gaps_opened:
                conn.execute(
                    "INSERT INTO gap_spans(source_id, identity, scope, started_at, ended_at) "
                    "VALUES(?,?,?,?,NULL)",
                    (source_id, identity, opened.scope, to_iso(opened.started_at)),
                )
            for event in plan.events:
                conn.execute(
                    "INSERT INTO events(source_id, identity, kind, occurred_at, details_json) "
                    "VALUES(?,?,?,?,?)",
                    (
                        source_id,
                        identity,
                        event.kind,
                        to_iso(event.occurred_at),
                        json.dumps(event.details, ensure_ascii=False),
                    ),
                )
            applied += 1
        if len(gap_pieces) <= GAP_MATERIALIZE_BATCH:
            for identity, hg in gap_pieces:
                _apply_hour_gap(conn, source_id, identity, hg)
        else:
            for identity, hg in gap_pieces[:GAP_MATERIALIZE_BATCH]:
                _apply_hour_gap(conn, source_id, identity, hg)
            _append_gap_queue(conn, [(source_id, ident, hg.scope, hg.hour) for ident, hg in gap_pieces[GAP_MATERIALIZE_BATCH:]])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return applied


def _append_gap_queue(conn: sqlite3.Connection, pieces: list[tuple[str, str, str, str]]) -> None:
    row = conn.execute("SELECT value FROM maintenance_cursor WHERE key=?", (GAP_QUEUE_KEY,)).fetchone()
    queue = json.loads(row[0]) if row else []
    queue.extend(pieces)
    conn.execute(
        "INSERT INTO maintenance_cursor(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (GAP_QUEUE_KEY, json.dumps(queue)),
    )


def drain_gap_materialization(conn: sqlite3.Connection, batch: int = GAP_MATERIALIZE_BATCH) -> int:
    """Materialize queued hourly gap rows in bounded, resumable batches.

    Returns the number of pieces still queued (0 = fully materialized).
    """
    row = conn.execute("SELECT value FROM maintenance_cursor WHERE key=?", (GAP_QUEUE_KEY,)).fetchone()
    if not row:
        return 0
    queue = json.loads(row[0])
    if not queue:
        return 0
    take, rest = queue[:batch], queue[batch:]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for src, identity, scope, hour in take:
            _apply_hour_gap(conn, src, identity, HourGap(scope, hour, 0.0))
        if rest:
            conn.execute(
                "INSERT INTO maintenance_cursor(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (GAP_QUEUE_KEY, json.dumps(rest)),
            )
        else:
            conn.execute("DELETE FROM maintenance_cursor WHERE key=?", (GAP_QUEUE_KEY,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rest)


def _opt_iso(value: datetime | None) -> str | None:
    return None if value is None else to_iso(value)


def _apply_hour_delta(conn: sqlite3.Connection, source_id: str, identity: str, hd: HourDelta) -> None:
    row = conn.execute(
        "SELECT tokens, quality_json, first_observed_at, last_observed_at, metadata_complete "
        "FROM hourly_totals WHERE source_id=? AND identity=? AND hour=? AND counter_key=?",
        (source_id, identity, hd.hour, hd.counter_key),
    ).fetchone()
    current = row[0] if row else None
    try:
        total = checked_add_int64(0 if current is None else current, hd.tokens)
    except OverflowError as exc:
        raise StorageError(
            "hourly_overflow",
            f"hourly total overflow for {identity} {hd.hour} {hd.counter_key}",
        ) from exc
    # The hourly row carries the union of its contributing intervals' quality
    # and coverage bounds, so quality survives raw-sample retention (plan D1).
    quality = merge_quality(row[1] if row else "[]", set(hd.quality))
    observed = None if hd.observed_at is None else to_iso(hd.observed_at)
    complete = bool(observed is not None and (row[4] if row else True))
    conn.execute(
        "INSERT INTO hourly_totals(source_id, identity, hour, counter_key, tokens, "
        "observed_intervals, quality_json, first_observed_at, last_observed_at, metadata_complete) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source_id, identity, hour, counter_key) DO UPDATE SET "
        "tokens=excluded.tokens, observed_intervals=hourly_totals.observed_intervals + ?, "
        "quality_json=excluded.quality_json, "
        "first_observed_at=COALESCE(MIN(hourly_totals.first_observed_at, excluded.first_observed_at), "
        "hourly_totals.first_observed_at, excluded.first_observed_at), "
        "last_observed_at=COALESCE(MAX(hourly_totals.last_observed_at, excluded.last_observed_at), "
        "hourly_totals.last_observed_at, excluded.last_observed_at), "
        "metadata_complete=excluded.metadata_complete",
        (
            source_id,
            identity,
            hd.hour,
            hd.counter_key,
            total,
            hd.observed_intervals,
            quality,
            observed,
            observed,
            int(complete),
            hd.observed_intervals,
        ),
    )


def _primary_pair_observed_at(plan: SeriesPlan) -> datetime | None:
    """When this plan last observed both primary counters, else None.

    A brand-new series has no sample row: its opening pair is recorded on the
    epoch, and that opening observation is still a valid presence observation
    for the series list (spec D6).
    """
    if plan.sample is not None:
        counters = plan.sample.counters
        observed_at = plan.sample.observed_at
    elif plan.new_epoch is not None:
        counters = {
            PRIMARY_KEYS[0]: plan.new_epoch.opening_computed_input,
            PRIMARY_KEYS[1]: plan.new_epoch.opening_generated_output,
        }
        observed_at = plan.new_epoch.started_at
    else:
        return None
    if counters.get(PRIMARY_KEYS[0]) is None or counters.get(PRIMARY_KEYS[1]) is None:
        return None
    return observed_at


def _apply_latest_interval(conn: sqlite3.Connection, source_id: str, identity: str, hd: HourDelta) -> None:
    """Persist the newest accepted interval so rates outlive raw retention (D4)."""
    if hd.observed_at is None:
        return
    conn.execute(
        "INSERT INTO latest_interval(source_id, identity, counter_key, observed_at, tokens, "
        "interval_seconds, eligible, quality_json) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source_id, identity, counter_key) DO UPDATE SET "
        "observed_at=excluded.observed_at, tokens=excluded.tokens, "
        "interval_seconds=excluded.interval_seconds, eligible=excluded.eligible, "
        "quality_json=excluded.quality_json",
        (
            source_id,
            identity,
            hd.counter_key,
            to_iso(hd.observed_at),
            hd.tokens,
            hd.interval_seconds,
            int(hd.eligible),
            json.dumps(sorted(hd.quality), ensure_ascii=False),
        ),
    )


def _apply_hour_gap(conn: sqlite3.Connection, source_id: str, identity: str, hg: HourGap) -> None:
    # A gap degrades exactly the counters sharing its scope (plan D1): the
    # coupled primary pair, or one optional counter — never the whole series.
    for counter_key in COUNTERS_FOR_SCOPE.get(hg.scope, (hg.scope,)):
        row = conn.execute(
            "SELECT quality_json, metadata_complete FROM hourly_totals WHERE source_id=? AND "
            "identity=? AND hour=? AND counter_key=?",
            (source_id, identity, hg.hour, counter_key),
        ).fetchone()
        # Gap-only bucket has NULL tokens; an existing trusted total is preserved.
        quality = merge_quality(row[0] if row else "[]", {QualityFlag.COLLECTION_GAP})
        conn.execute(
            "INSERT INTO hourly_totals(source_id, identity, hour, counter_key, tokens, "
            "observed_intervals, quality_json, first_observed_at, last_observed_at, metadata_complete) "
            "VALUES(?,?,?,?,NULL,0,?,?,?,?) "
            "ON CONFLICT(source_id, identity, hour, counter_key) DO UPDATE SET "
            "quality_json=excluded.quality_json",
            (
                source_id,
                identity,
                hg.hour,
                counter_key,
                quality,
                None,
                None,
                int(row[1]) if row else 1,
            ),
        )


# ---------------------------------------------------------------------------
# Bounded, resumable 0002 backfill (Delivery 2 plan Step 2)
# ---------------------------------------------------------------------------


def _backfill_state(conn: sqlite3.Connection) -> dict[str, object]:
    row = conn.execute(
        "SELECT value FROM maintenance_cursor WHERE key=?", (API_METADATA_BACKFILL_KEY,)
    ).fetchone()
    return json.loads(row[0]) if row else {"seq": False, "hourly": None, "interval": None, "presence": False}


def _save_backfill_state(conn: sqlite3.Connection, state: dict[str, object]) -> None:
    if all(state[k] in (True, None) for k in state):
        conn.execute("DELETE FROM maintenance_cursor WHERE key=?", (API_METADATA_BACKFILL_KEY,))
        return
    conn.execute(
        "INSERT INTO maintenance_cursor(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (API_METADATA_BACKFILL_KEY, json.dumps(state)),
    )


def backfill_api_metadata(conn: sqlite3.Connection, *, batch: int = BACKFILL_BATCH) -> int:
    """Advance the 0002 legacy backfill by at most ``batch`` rows per phase.

    Reconstructs coverage from retained raw samples, epochs and gap spans. Rows
    whose raw evidence is already gone are marked ``metadata_complete = 0`` with
    null bounds — totals and observed intervals are never modified. Returns the
    number of phase batches still to run (0 = complete). Runs in maintenance,
    never on the API read path.
    """
    state = _backfill_state(conn)
    remaining = 0
    remaining += _backfill_seq(conn, state, batch)
    remaining += _backfill_hourly(conn, state, batch)
    remaining += _backfill_intervals(conn, state, batch)
    remaining += _backfill_presence(conn, state, batch)
    conn.execute("BEGIN IMMEDIATE")
    _save_backfill_state(conn, state)
    conn.commit()
    return remaining


def run_backfills(conn: sqlite3.Connection, *, batch: int = BACKFILL_BATCH, max_rounds: int = 100_000) -> int:
    """Run the backfill to completion; returns rounds used (maintenance use)."""
    rounds = 0
    while rounds < max_rounds and backfill_api_metadata(conn, batch=batch):
        rounds += 1
    return rounds


def _backfill_seq(conn: sqlite3.Connection, state: dict[str, object], batch: int) -> int:
    """Assign the stable enumeration key to series that predate it."""
    if state["seq"]:
        return 0
    rows = conn.execute(
        "SELECT rowid FROM series WHERE seq IS NULL ORDER BY first_seen_at, rowid LIMIT ?",
        (batch,),
    ).fetchall()
    if rows:
        base = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM series").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        for offset, (rowid,) in enumerate(rows, start=1):
            conn.execute("UPDATE series SET seq=? WHERE rowid=?", (base + offset, rowid))
        conn.commit()
    remaining = conn.execute("SELECT count(*) FROM series WHERE seq IS NULL").fetchone()[0]
    state["seq"] = remaining == 0
    return 1 if remaining else 0


def _backfill_hourly(conn: sqlite3.Connection, state: dict[str, object], batch: int) -> int:
    cursor = state["hourly"]
    if cursor is None and state.get("hourly_done"):
        return 0
    start = cursor or ["", "", "", ""]
    # Only legacy rows (created by migration 0002, before the writer learned to
    # record bounds) are eligible. Writer-written rows are never revisited: raw
    # samples age out through retention, and re-deriving them would erase real
    # coverage metadata.
    rows = conn.execute(
        "SELECT source_id, identity, hour, counter_key, observed_intervals, metadata_complete, "
        "quality_json FROM hourly_totals WHERE metadata_complete = 0 "
        "AND (source_id, identity, hour, counter_key) > (?,?,?,?) "
        "ORDER BY source_id, identity, hour, counter_key LIMIT ?",
        (*start, batch),
    ).fetchall()
    if rows:
        conn.execute("BEGIN IMMEDIATE")
        for source_id, identity, hour, counter_key, intervals, complete, quality_json in rows:
            _backfill_hour_row(conn, source_id, identity, hour, counter_key, intervals, complete, quality_json)
        conn.commit()
    if rows:
        state["hourly"] = [rows[-1][0], rows[-1][1], rows[-1][2], rows[-1][3]]
    if len(rows) < batch:
        state["hourly"] = None
        state["hourly_done"] = True
        return 0
    return 1


def _backfill_hour_row(
    conn: sqlite3.Connection,
    source_id: str,
    identity: str,
    hour: str,
    counter_key: str,
    intervals: int,
    complete: int,
    quality_json: str,
) -> None:
    if intervals == 0:
        # Gap-only bucket: no contributions, so nothing is missing.
        conn.execute(
            "UPDATE hourly_totals SET metadata_complete=1 WHERE source_id=? AND identity=? "
            "AND hour=? AND counter_key=?",
            (source_id, identity, hour, counter_key),
        )
        return
    column = f"delta_{counter_key}"
    evidence = conn.execute(
        f"SELECT observed_at, quality_json FROM samples WHERE source_id=? AND identity=? "
        f"AND substr(observed_at,1,13)=? AND {column} IS NOT NULL ORDER BY observed_at",  # noqa: S608
        (source_id, identity, hour[:-1]),
    ).fetchall()
    if not evidence:
        # Raw coverage was retained away before this migration: bounds are
        # unknown forever and are never invented (plan Step 2).
        conn.execute(
            "UPDATE hourly_totals SET metadata_complete=0, first_observed_at=NULL, "
            "last_observed_at=NULL WHERE source_id=? AND identity=? AND hour=? AND counter_key=?",
            (source_id, identity, hour, counter_key),
        )
        return
    flags = {QualityFlag.COLLECTION_GAP} if QualityFlag.COLLECTION_GAP in json.loads(quality_json) else set()
    for _at, sample_quality in evidence:
        flags.update(json.loads(sample_quality or "[]"))
    conn.execute(
        "UPDATE hourly_totals SET metadata_complete=?, first_observed_at=?, last_observed_at=?, "
        "quality_json=? WHERE source_id=? AND identity=? AND hour=? AND counter_key=?",
        (
            1,
            evidence[0][0],
            evidence[-1][0],
            merge_quality(quality_json, flags),
            source_id,
            identity,
            hour,
            counter_key,
        ),
    )


def _backfill_intervals(conn: sqlite3.Connection, state: dict[str, object], batch: int) -> int:
    cursor = state["interval"]
    if cursor is None and state.get("interval_done"):
        return 0
    rows = conn.execute(
        "SELECT source_id, identity, rowid FROM series WHERE rowid > ? ORDER BY rowid LIMIT ?",
        (cursor or 0, batch),
    ).fetchall()
    if rows:
        conn.execute("BEGIN IMMEDIATE")
    for source_id, identity, rowid in rows:
        for counter_key in COUNTER_KEYS:
            recent = conn.execute(
                f"SELECT observed_at, delta_{counter_key}, quality_json, rate_eligible FROM samples "  # noqa: S608
                f"WHERE source_id=? AND identity=? AND delta_{counter_key} IS NOT NULL "
                "ORDER BY observed_at DESC, sample_id DESC LIMIT 2",
                (source_id, identity),
            ).fetchall()
            if not recent:
                continue
            newest_at, delta, quality, eligible = recent[0]
            seconds = None
            if len(recent) > 1:
                seconds = (
                    parse_iso(newest_at) - parse_iso(recent[1][0])
                ).total_seconds()
            conn.execute(
                "INSERT INTO latest_interval(source_id, identity, counter_key, observed_at, tokens, "
                "interval_seconds, eligible, quality_json) SELECT ?,?,?,?,?,?,?,? "
                "WHERE NOT EXISTS (SELECT 1 FROM latest_interval WHERE source_id=? AND identity=? "
                "AND counter_key=?)",
                (
                    source_id,
                    identity,
                    counter_key,
                    newest_at,
                    delta,
                    seconds,
                    eligible,
                    quality,
                    source_id,
                    identity,
                    counter_key,
                ),
            )
    conn.commit()
    if rows:
        state["interval"] = rows[-1][2]
    if len(rows) < batch:
        state["interval"] = None
        state["interval_done"] = True
        return 0
    return 1


def _backfill_presence(conn: sqlite3.Connection, state: dict[str, object], batch: int) -> int:
    if state["presence"]:
        return 0
    rows = conn.execute(
        "SELECT rowid, source_id, identity FROM series WHERE first_valid_at IS NULL "
        "ORDER BY rowid LIMIT ?",
        (batch,),
    ).fetchall()
    conn.execute("BEGIN IMMEDIATE")
    for rowid, source_id, identity in rows:
        bounds = conn.execute(
            "SELECT min(observed_at), max(observed_at) FROM ("
            "SELECT observed_at FROM samples WHERE source_id=? AND identity=? "
            "AND computed_input IS NOT NULL AND generated_output IS NOT NULL "
            "UNION ALL "
            "SELECT started_at FROM epochs WHERE source_id=? AND identity=? "
            "AND opening_computed_input IS NOT NULL AND opening_generated_output IS NOT NULL)",
            (source_id, identity, source_id, identity),
        ).fetchone()
        if bounds[0] is None:
            continue  # raw evidence already retained away: presence stays unknown
        conn.execute(
            "UPDATE series SET first_valid_at=?, last_valid_at=? WHERE rowid=?",
            (bounds[0], bounds[1], rowid),
        )
    conn.commit()
    state["presence"] = True
    return 1 if len(rows) == batch else 0


def snapshot_metadata_counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT count(*), COALESCE(sum(metadata_complete), 0) FROM hourly_totals"
    ).fetchone()
    return {"rows": row[0], "complete": row[1]}


def purge_expired(
    conn: sqlite3.Connection, cutoff_iso: str, *, batch: int = 500
) -> int:
    """Delete raw samples and events older than the cutoff (hourly data kept).

    Batched in its own transaction; returns the number of rows removed.
    """
    removed = 0
    while True:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM samples WHERE sample_id IN (SELECT sample_id FROM samples "
            "WHERE observed_at < ? LIMIT ?)",
            (cutoff_iso, batch),
        )
        removed += cur.rowcount
        conn.commit()
        if cur.rowcount == 0:
            break
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute("DELETE FROM events WHERE occurred_at < ?", (cutoff_iso,))
    removed += cur.rowcount
    conn.commit()
    conn.execute(
        "INSERT INTO maintenance_cursor(key, value) VALUES('retention_cutoff', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (cutoff_iso,),
    )
    return removed
