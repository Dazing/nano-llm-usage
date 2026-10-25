"""Durable SQLite storage: volume lock, schema migration, atomic poll application.

All mutations are single-writer behind an exclusive lock file. A poll is
applied in one transaction so baselines, samples, hourly totals, gaps and
events commit together (all-or-none).
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from .accounting import HourDelta, HourGap, SeriesPlan
from .domain import (
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

CURRENT_SCHEMA_VERSION = 1
# Max hourly gap rows materialized per transaction (resumable beyond that).
GAP_MATERIALIZE_BATCH = 500
GAP_QUEUE_KEY = "gap_materialization_queue"
LAST_RETENTION_KEY = "last_retention_at"
_SQLITE_HEADER = b"SQLite format 3\0"

RUNTIME_STORAGE_STATUS = "storage_status"
RUNTIME_COLLECTOR_STATUS = "collector_status"
RUNTIME_LAST_POLL_AT = "last_poll_at"
RUNTIME_LAST_ERROR_CODE = "last_error_code"


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


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-16000")
    return conn


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
        if version > 0:
            snapshot = db_path.with_name(f"{db_path.name}.pre-migration-{number}.sqlite3")
            dest = sqlite3.connect(snapshot)
            with dest:
                conn.backup(dest)
            dest.close()
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
    return conn


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
) -> None:
    at_iso = to_iso(at)
    conn.execute(
        "INSERT INTO sources(source_id, endpoint_fingerprint, first_seen_at, last_seen_at, "
        "last_attempt_at, last_error_code) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(source_id) DO UPDATE SET endpoint_fingerprint=excluded.endpoint_fingerprint, "
        "last_seen_at=excluded.last_seen_at, last_attempt_at=excluded.last_attempt_at, "
        "last_error_code=excluded.last_error_code",
        (source_id, endpoint_fingerprint, at_iso, at_iso, at_iso, error_code),
    )


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
) -> int:
    """Atomically apply one poll's plans. Returns number of series applied.

    Idempotent per ``poll_id``: a replayed poll applies nothing (D25).
    Any overflow/integrity failure rolls the whole poll back so no baseline
    advances (D24/C22/D28).
    """
    applied = 0
    gap_pieces: list[tuple[str, HourGap]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        at_iso = to_iso(observed_at)
        record_source_attempt(conn, source_id, endpoint_fingerprint, observed_at, None)
        for plan in plans:
            identity = canonical_labels_json(plan.labels)
            if conn.execute(
                "SELECT 1 FROM samples WHERE source_id=? AND identity=? AND applied_key=?",
                (source_id, identity, poll_id),
            ).fetchone():
                continue
            at_iso_s = at_iso
            if plan.is_new:
                conn.execute(
                    "INSERT INTO series(source_id, identity, labels_json, first_seen_at, "
                    "last_seen_at, stale_since) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(source_id, identity) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (
                        source_id,
                        identity,
                        json.dumps(plan.labels, sort_keys=True, ensure_ascii=False),
                        at_iso_s,
                        at_iso_s,
                        _opt_iso(plan.state.stale_since),
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
        "SELECT tokens, quality_json FROM hourly_totals WHERE source_id=? AND identity=? "
        "AND hour=? AND counter_key=?",
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
    quality = merge_quality(row[1] if row else "[]", set())
    conn.execute(
        "INSERT INTO hourly_totals(source_id, identity, hour, counter_key, tokens, "
        "observed_intervals, quality_json) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(source_id, identity, hour, counter_key) DO UPDATE SET "
        "tokens=excluded.tokens, observed_intervals=hourly_totals.observed_intervals + ?, "
        "quality_json=excluded.quality_json",
        (source_id, identity, hd.hour, hd.counter_key, total, hd.observed_intervals, quality,
         hd.observed_intervals),
    )


def _apply_hour_gap(conn: sqlite3.Connection, source_id: str, identity: str, hg: HourGap) -> None:
    row = conn.execute(
        "SELECT tokens, quality_json FROM hourly_totals WHERE source_id=? AND identity=? "
        "AND hour=? AND counter_key=?",
        (source_id, identity, hg.hour, "computed_input"),
    ).fetchone()
    # Gap-only bucket has NULL tokens; an existing trusted total is preserved.
    quality = merge_quality(row[1] if row else "[]", {QualityFlag.COLLECTION_GAP})
    conn.execute(
        "INSERT INTO hourly_totals(source_id, identity, hour, counter_key, tokens, "
        "observed_intervals, quality_json) VALUES(?,?,?,?,NULL,0,?) "
        "ON CONFLICT(source_id, identity, hour, counter_key) DO UPDATE SET "
        "quality_json=excluded.quality_json",
        (source_id, identity, hg.hour, "computed_input", quality),
    )


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
