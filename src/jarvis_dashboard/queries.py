"""Read-only snapshot queries that back the HTTP API (Delivery 2, spec D4/D6).

Every entry point opens exactly one read-only SQLite snapshot (one read
transaction for the whole response) through :func:`storage.read_snapshot`, so a
response is internally consistent even while the collector writes. Nothing here
writes: gap overlay, gap seconds, and rate eligibility are read from the durable
records the writer owns.

Contract rules honoured here:
- Token values are exact integers summed with checked signed-int64 addition;
  overflow is reported, never coerced to float or wrapped.
- Buckets are generated from aligned UTC boundaries; a bucket with no observed
  intervals is ``null`` (no data), a bucket with observed intervals and zero
  tokens is ``"0"`` (real zero).
- Gap seconds come from authoritative ``gap_spans`` clipped to the bucket and to
  the request time, unioned per counter scope, plus the implicit downtime that
  starts after two poll intervals beyond the last successful observation.
- Legacy rows whose contributing observations were purged before migration are
  reported with ``metadata_complete: false`` and null bounds; coverage is never
  invented.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from . import storage
from .config import CostRates
from .domain import (
    CACHED_INPUT,
    COMPUTED_INPUT,
    COUNTER_KEYS,
    GENERATED_OUTPUT,
    QUALITY_FLAGS,
    REQUEST_INPUT,
    REQUEST_OUTPUT,
    SCOPE_FOR_COUNTER,
    SCOPES,
    checked_add_int64,
    hour_floor,
    hour_label,
    labels_from_canonical,
    parse_iso,
    series_display,
    to_iso,
)
from .lifecycle import HealthSnapshot

CURSOR_VERSION = "v1"
MAX_CURSOR_CHARS = 512
MAX_EPOCH_PAGE = 20
IMPLICIT_GAP_INTERVALS = 2  # downtime starts strictly after two poll intervals

BUCKET_SECONDS = {"hour": 3600, "day": 86400}
MAX_BUCKETS = {"hour": 744, "day": 366}

# API-equivalent cost (cost plan D4): read-time only, over hours where every
# priced counter was observed. Money is never stored, so a rate change reprices.
PRICED_COUNTERS = (REQUEST_INPUT, REQUEST_OUTPUT, CACHED_INPUT)
COST_CURRENCY = "USD"
CACHE_EXCEEDS_INPUT = "cache_exceeds_input"
_MILLION = Decimal(1_000_000)
_AMOUNT_PLACES = Decimal("0.000001")
_HOUR = timedelta(hours=1)


class QueryError(Exception):
    """A query failure the API layer maps to a stable structured error."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# identifiers and cursors
# --------------------------------------------------------------------------- #
def encode_series_id(source_id: str, identity: str) -> str:
    payload = json.dumps([source_id, identity], separators=(",", ":"), ensure_ascii=True)
    return f"{CURSOR_VERSION}.{_b64(payload.encode('utf-8'))}"


def decode_series_id(raw: str) -> tuple[str, str]:
    """Decode a ``series_id``; malformed values are the caller's 400."""
    version, _, encoded = raw.partition(".")
    if version != CURSOR_VERSION or not encoded or len(raw) > MAX_CURSOR_CHARS * 4:
        raise QueryError("invalid_series_id", "series_id is not a valid identifier")
    try:
        payload = json.loads(_unb64(encoded).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, binascii.Error) as exc:
        raise QueryError("invalid_series_id", "series_id is not a valid identifier") from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 2
        or not all(isinstance(part, str) and part for part in payload)
    ):
        raise QueryError("invalid_series_id", "series_id is not a valid identifier")
    source_id, identity = payload
    try:
        labels = labels_from_canonical(identity)
    except (ValueError, TypeError) as exc:
        raise QueryError("invalid_series_id", "series_id is not a valid identifier") from exc
    # The identity must be the canonical form of its own labels: a hand-made
    # identifier with reordered or non-canonical JSON is rejected, not resolved.
    canonical = json.dumps(
        labels, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    if canonical != identity:
        raise QueryError("invalid_series_id", "series_id is not a valid identifier")
    return source_id, identity


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"{CURSOR_VERSION}.{_b64(raw)}"


def decode_cursor(raw: str, *, route: str, binding: dict[str, Any]) -> dict[str, Any]:
    """Decode a cursor and prove it belongs to this route and filter."""
    version, _, encoded = raw.partition(".")
    if version != CURSOR_VERSION or not encoded or len(raw) > MAX_CURSOR_CHARS:
        raise QueryError("invalid_cursor", "cursor is not valid for this request")
    try:
        payload = json.loads(_unb64(encoded).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, binascii.Error) as exc:
        raise QueryError("invalid_cursor", "cursor is not valid for this request") from exc
    if not isinstance(payload, dict) or payload.get("r") != route:
        raise QueryError("invalid_cursor", "cursor is not valid for this request")
    for key, value in binding.items():
        if payload.get(key) != value:
            raise QueryError("invalid_cursor", "cursor is not valid for this request")
    return payload


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def parse_utc(raw: str) -> datetime:
    """Accept RFC 3339 UTC only (``Z`` or a zero offset); naive is rejected."""
    if not isinstance(raw, str) or not raw:
        raise QueryError("invalid_time", "timestamps must be RFC 3339 UTC")
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise QueryError("invalid_time", "timestamps must be RFC 3339 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise QueryError("invalid_time", "timestamps must be RFC 3339 UTC with a zero offset")
    return parsed.replace(microsecond=parsed.microsecond // 1000 * 1000, tzinfo=timezone.utc)


def parse_range(raw_from: str, raw_to: str, bucket: str) -> tuple[datetime, datetime, int]:
    """Aligned, bounded, half-open ``[from, to)`` range for a bucket size."""
    if bucket not in BUCKET_SECONDS:
        raise QueryError("invalid_bucket", "bucket must be 'hour' or 'day'")
    start, end = parse_utc(raw_from), parse_utc(raw_to)
    seconds = BUCKET_SECONDS[bucket]
    if start >= end:
        raise QueryError("invalid_range", "'from' must be earlier than 'to'")
    if _epoch(start) % seconds or _epoch(end) % seconds:
        raise QueryError(
            "unaligned_range", f"'from' and 'to' must align to a {bucket} boundary"
        )
    if (end - start).total_seconds() // seconds > MAX_BUCKETS[bucket]:
        raise QueryError(
            "range_too_large",
            f"a {bucket} history request may span at most {MAX_BUCKETS[bucket]} buckets",
        )
    return start, end, seconds


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _floor(moment: datetime, seconds: int) -> datetime:
    return datetime.fromtimestamp(_epoch(moment) - _epoch(moment) % seconds, timezone.utc)


def _day_floor(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket_label(start: datetime, seconds: int) -> str:
    return start.strftime("%Y-%m-%d") if seconds == 86400 else hour_label(start)


def _bucket_bounds(label: str, seconds: int) -> tuple[datetime, datetime]:
    if seconds == 86400:
        start = datetime.strptime(label, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return start, start + timedelta(days=1)
    start = datetime.strptime(label, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    return start, start + timedelta(hours=1)


def _sum_int64(values: list[int]) -> int:
    total = 0
    for value in values:
        total = checked_add_int64(total, value)
    return total


# --------------------------------------------------------------------------- #
# shared read plumbing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Series:
    identity: str
    series_id: str
    labels: dict[str, str]
    model_name: str
    engine: str
    first_seen_at: str | None
    last_seen_at: str | None
    first_valid_at: str | None
    last_valid_at: str | None
    stale_since: str | None
    seq: int | None


def _open(db_path: Path | str) -> Any:
    try:
        return storage.read_snapshot(Path(db_path))
    except (storage.StorageError, sqlite3.Error) as exc:  # pragma: no cover - guard
        raise QueryError("storage_unavailable", "storage is not readable", 503) from exc


def _resolve(conn: sqlite3.Connection, source_id: str, series_id: str) -> _Series:
    want_source, identity = decode_series_id(series_id)
    if want_source != source_id:
        # A valid identifier for another source is not this source's series.
        raise QueryError("series_not_found", "series not found", 404)
    row = conn.execute(
        "SELECT labels_json, first_seen_at, last_seen_at, first_valid_at,"
        " last_valid_at, stale_since, seq FROM series WHERE source_id = ? AND identity = ?",
        (source_id, identity),
    ).fetchone()
    if row is None:
        raise QueryError("series_not_found", "series not found", 404)
    labels = labels_from_canonical(identity)
    model_name, engine = series_display(labels)
    return _Series(
        identity=identity,
        series_id=series_id,
        labels=labels,
        model_name=model_name,
        engine=engine,
        first_seen_at=row[0] and row[1],
        last_seen_at=row[2],
        first_valid_at=row[3],
        last_valid_at=row[4],
        stale_since=row[5],
        seq=row[6],
    )


def _gap_spans(
    conn: sqlite3.Connection,
    source_id: str,
    identity: str,
    scope: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime | None]]:
    rows = conn.execute(
        "SELECT started_at, ended_at FROM gap_spans"
        " WHERE source_id = ? AND identity = ? AND scope = ?"
        "   AND started_at < ? AND (ended_at IS NULL OR ended_at > ?)",
        (source_id, identity, scope, to_iso(end), to_iso(start)),
    ).fetchall()
    return [(parse_iso(r[0]), None if r[1] is None else parse_iso(r[1])) for r in rows]


def _clip_seconds(
    spans: list[tuple[datetime, datetime | None]],
    start: datetime,
    end: datetime,
) -> float:
    """Union of ``spans`` clipped to ``[start, end)`` (open spans end at ``end``)."""
    pieces: list[tuple[float, float]] = []
    for opened, closed in spans:
        lo = max(opened, start)
        hi = min(closed, end) if closed is not None else end
        if hi > lo:
            pieces.append((lo.timestamp(), hi.timestamp()))
    return _union_seconds(pieces)


def _union_seconds(pieces: list[tuple[float, float]]) -> float:
    if not pieces:
        return 0.0
    pieces.sort()
    total = 0.0
    cur_lo, cur_hi = pieces[0]
    for lo, hi in pieces[1:]:
        if lo > cur_hi:
            total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    return total + cur_hi - cur_lo


def _implicit_downtime(
    last_success_at: str | None, now: datetime, poll_interval_seconds: int
) -> list[tuple[datetime, datetime | None]]:
    """Downtime beyond two poll intervals after the last successful collection.

    The first two intervals are tolerated (the exact-two boundary is preserved:
    downtime starts *after* two intervals, not at it).
    """
    if not last_success_at:
        return []
    last = parse_iso(last_success_at)
    threshold = last + timedelta(seconds=poll_interval_seconds * IMPLICIT_GAP_INTERVALS)
    if now <= threshold:
        return []
    return [(threshold, None)]


def _scope_gap_seconds(
    conn: sqlite3.Connection,
    source_id: str,
    identity: str,
    scope: str,
    start: datetime,
    end: datetime,
    now: datetime,
    poll_interval_seconds: int,
    last_success_at: str | None,
) -> float:
    spans = _gap_spans(conn, source_id, identity, scope, start, end)
    spans = [(lo, hi) for lo, hi in spans if lo < end and (hi is None or hi > start)]
    clipped = _clip_seconds(spans, start, end)
    implicit = _clip_seconds(
        _implicit_downtime(last_success_at, now, poll_interval_seconds), start, end
    )
    # Authoritative spans and the implicit overlay describe the same outage:
    # union them, never add them.
    pieces = _overlap_pieces(spans, start, end) + _overlap_pieces(
        _implicit_downtime(last_success_at, now, poll_interval_seconds), start, end
    )
    return max(clipped, implicit, _union_seconds(pieces))


def _overlap_pieces(
    spans: list[tuple[datetime, datetime | None]], start: datetime, end: datetime
) -> list[tuple[float, float]]:
    pieces: list[tuple[float, float]] = []
    for opened, closed in spans:
        lo = max(opened, start)
        hi = min(closed, end) if closed is not None else end
        if hi > lo:
            pieces.append((lo.timestamp(), hi.timestamp()))
    return pieces


# --------------------------------------------------------------------------- #
# hourly aggregation
# --------------------------------------------------------------------------- #
def _hourly_rows(
    conn: sqlite3.Connection,
    source_id: str,
    identity: str,
    label_from: str,
    label_to: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    """{bucket label: {counter_key: row}} for the half-open label range."""
    rows = conn.execute(
        "SELECT hour, counter_key, tokens, observed_intervals, quality_json,"
        " first_observed_at, last_observed_at, metadata_complete"
        " FROM hourly_totals"
        " WHERE source_id = ? AND identity = ? AND hour >= ? AND hour < ?"
        " ORDER BY hour, counter_key",
        (source_id, identity, label_from, label_to),
    ).fetchall()
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for hour, key, tokens, intervals, quality, first_at, last_at, complete in rows:
        grouped.setdefault(hour, {})[key] = {
            "tokens": tokens,
            "observed_intervals": int(intervals or 0),
            "quality": _json_list(quality),
            "first_observed_at": first_at,
            "last_observed_at": last_at,
            "metadata_complete": bool(complete),
        }
    return grouped


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _counter_block(
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Null when nothing was observed; '0' when intervals were observed."""
    if row is None:
        return {
            "tokens": None,
            "observed_intervals": 0,
            "first_observed_at": None,
            "last_observed_at": None,
            "gap_seconds": 0.0,
            "quality_flags": [],
            "metadata_complete": True,
        }
    intervals = row["observed_intervals"]
    tokens = row["tokens"]
    return {
        "tokens": str(tokens) if (intervals > 0 and tokens is not None) else None,
        "observed_intervals": intervals,
        "first_observed_at": row["first_observed_at"],
        "last_observed_at": row["last_observed_at"],
        "gap_seconds": 0.0,
        "quality_flags": [flag for flag in QUALITY_FLAGS if flag in row["quality"]],
        "metadata_complete": row["metadata_complete"],
    }


def _merge_counters(
    blocks: dict[str, dict[str, Any]], rows: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Fold bucket rows into a running per-counter block."""
    for key in COUNTER_KEYS:
        row = rows.get(key)
        if row is None:
            continue
        current = blocks.get(key)
        if current is None:
            current = _counter_block(None)
            blocks[key] = current
        current["observed_intervals"] += row["observed_intervals"]
        present = row["observed_intervals"] > 0 and row["tokens"] is not None
        if present:
            current["tokens"] = str(
                checked_add_int64(int(current["tokens"] or 0), int(row["tokens"]))
            )
        elif current["tokens"] is None and current["observed_intervals"] == 0:
            current["tokens"] = None
        current["first_observed_at"] = _min_iso(
            current["first_observed_at"], row["first_observed_at"]
        )
        current["last_observed_at"] = _max_iso(
            current["last_observed_at"], row["last_observed_at"]
        )
        flags = set(current["quality_flags"]) | {
            flag for flag in QUALITY_FLAGS if flag in row["quality"]
        }
        current["quality_flags"] = [flag for flag in QUALITY_FLAGS if flag in flags]
        current["metadata_complete"] = current["metadata_complete"] and row[
            "metadata_complete"
        ]
    for key, current in blocks.items():
        if current["observed_intervals"] == 0:
            current["tokens"] = None
    return blocks


def _min_iso(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_iso(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _processed_total(blocks: dict[str, dict[str, Any]]) -> str | None:
    """Processed total: only when both primary counters have observed intervals."""
    ci = blocks.get(COMPUTED_INPUT)
    go = blocks.get(GENERATED_OUTPUT)
    if not ci or not go:
        return None
    if ci["observed_intervals"] == 0 or go["observed_intervals"] == 0:
        return None
    if ci["tokens"] is None or go["tokens"] is None:
        return None
    return str(checked_add_int64(int(ci["tokens"]), int(go["tokens"])))


def _blocks_payload(blocks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "counters": {key: blocks.get(key) or _counter_block(None) for key in COUNTER_KEYS}
    }
    payload["processed_total"] = _processed_total(blocks)
    flags = {flag for block in blocks.values() for flag in block["quality_flags"]}
    payload["quality_flags"] = [flag for flag in QUALITY_FLAGS if flag in flags]
    payload["metadata_complete"] = all(
        block["metadata_complete"] for block in blocks.values()
    )
    return payload


# --------------------------------------------------------------------------- #
# buckets and ranges
# --------------------------------------------------------------------------- #
def _scope_spans(
    conn: sqlite3.Connection, source_id: str, identity: str, start: datetime, end: datetime
) -> dict[str, list[tuple[datetime, datetime | None]]]:
    """Authoritative gap spans per scope overlapping ``[start, end)``."""
    rows = conn.execute(
        "SELECT scope, started_at, ended_at FROM gap_spans"
        " WHERE source_id = ? AND identity = ?"
        "   AND started_at < ? AND (ended_at IS NULL OR ended_at > ?)",
        (source_id, identity, to_iso(end), to_iso(start)),
    ).fetchall()
    spans: dict[str, list[tuple[datetime, datetime | None]]] = {}
    for scope, opened, closed in rows:
        spans.setdefault(scope, []).append(
            (parse_iso(opened), None if closed is None else parse_iso(closed))
        )
    return spans


def _gap_lookup(
    spans: dict[str, list[tuple[datetime, datetime | None]]],
    implicit: list[tuple[datetime, datetime | None]],
    start: datetime,
    end: datetime,
    now: datetime,
) -> dict[str, float]:
    """Gap seconds per scope over ``[start, min(end, now))``.

    Recorded spans and the implicit downtime overlay describe the same outage,
    so they are unioned; deferred hourly gap markers are never added as extra
    durations.
    """
    cut = min(end, now)
    gaps: dict[str, float] = {}
    for scope in SCOPES:
        pieces = _overlap_pieces(spans.get(scope, []), start, cut) + _overlap_pieces(
            implicit, start, cut
        )
        gaps[scope] = round(_union_seconds(pieces), 6)
    return gaps


def _gap_seconds_for_counter(
    gaps: dict[str, float], counter_key: str
) -> float:
    return gaps.get(SCOPE_FOR_COUNTER[counter_key], 0.0)


PARTIAL_FLAGS = frozenset({"collection_gap", "counter_unavailable", "clock_discontinuity"})


def _bucket_payloads(
    conn: sqlite3.Connection,
    source_id: str,
    series: _Series,
    start: datetime,
    end: datetime,
    seconds: int,
    now: datetime,
    poll_interval_seconds: int,
    last_success_at: str | None,
) -> list[dict[str, Any]]:
    grouped = _hourly_rows(
        conn, source_id, series.identity, _bucket_label(start, seconds), _bucket_label(end, seconds)
    )
    members = _member_labels(start, seconds)
    spans = _scope_spans(conn, source_id, series.identity, start, end)
    implicit = _implicit_downtime(last_success_at, now, poll_interval_seconds)
    buckets: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        bucket_end = cursor + timedelta(seconds=seconds)
        blocks: dict[str, dict[str, Any]] = {}
        for label in members(cursor):
            blocks = _merge_counters(blocks, grouped.get(label, {}))
        gaps = _gap_lookup(spans, implicit, cursor, bucket_end, now)
        payload = _blocks_payload(blocks)
        # Every counter carries its scope's gap, including counters with no
        # rows at all in this bucket: an unobserved counter is exactly what a
        # gap means for it.
        for key, block in payload["counters"].items():
            block["gap_seconds"] = _gap_seconds_for_counter(gaps, key)
        payload.update(
            {
                "bucket": _bucket_label(cursor, seconds),
                "started_at": to_iso(cursor),
                "ended_at": to_iso(bucket_end),
                "gap_seconds": {scope: gaps[scope] for scope in sorted(gaps)},
                "incomplete": bucket_end > now,
                "partial": _is_partial(payload, gaps),
            }
        )
        buckets.append(payload)
        cursor = bucket_end
    return buckets


def _member_labels(start: datetime, seconds: int):
    """Hourly row labels that fall inside one bucket of the requested size."""
    if seconds == 3600:
        return lambda cursor: [_bucket_label(cursor, 3600)]
    return lambda cursor: [
        _bucket_label(cursor + timedelta(hours=hour), 3600) for hour in range(24)
    ]


def _is_partial(payload: dict[str, Any], gaps: dict[str, float]) -> bool:
    if any(seconds > 0 for seconds in gaps.values()):
        return True
    if not payload["metadata_complete"]:
        return True
    return any(flag in PARTIAL_FLAGS for flag in payload["quality_flags"])


def _is_priced_hour(per_hour: dict[str, dict[str, Any]]) -> bool:
    """A hour is priced only when every priced counter was observed in it (D4)."""
    for key in PRICED_COUNTERS:
        row = per_hour.get(key)
        if row is None or row["observed_intervals"] <= 0 or row["tokens"] is None:
            return False
    return True


def _expected_hours(start: datetime, end: datetime, rows: dict[str, Any]) -> int:
    """Complete hours in ``[start, end)``, plus the current hour once it has rows.

    The trailing hour counts only after it has an hourly row, so a range never
    reads as "partial" in the first seconds of a new hour.
    """
    first = hour_floor(start)
    last = hour_floor(end)
    hours = int((last - first).total_seconds() // 3600)
    if hours < 0:
        hours = 0
    if hour_label(last) in rows:
        hours += 1
    return hours


def _api_equivalent(
    rows: dict[str, dict[str, dict[str, Any]]],
    start: datetime,
    end: datetime,
    rates: CostRates | None,
) -> dict[str, Any]:
    """API-equivalent cost, cache share and coverage for one range (cost plan D4).

    Only hours where the request counters *and* the cache counter were observed
    are priced, so a period before cache tracking contributes no money and no
    cache share; it lowers coverage instead. Nothing is stored: the answer is a
    function of the hourly rows and the rates configured at startup.
    """
    priced = [per_hour for per_hour in rows.values() if _is_priced_hour(per_hour)]
    request_input = cached_input = request_output = 0
    for per_hour in priced:
        request_input = checked_add_int64(
            request_input, int(per_hour[REQUEST_INPUT]["tokens"])
        )
        cached_input = checked_add_int64(cached_input, int(per_hour[CACHED_INPUT]["tokens"]))
        request_output = checked_add_int64(
            request_output, int(per_hour[REQUEST_OUTPUT]["tokens"])
        )
    uncached = request_input - cached_input
    flags: list[str] = []
    if uncached < 0:
        # AC5: cache tokens ahead of request tokens (timing skew). Price the
        # uncached part as zero and say so -- never clamp it silently.
        flags.append(CACHE_EXCEEDS_INPUT)
        uncached = 0
    amount = None
    ratio = None
    if priced:
        if rates is not None:
            total = (
                Decimal(uncached) * rates.input_per_mtok
                + Decimal(cached_input) * rates.cached_input_per_mtok
                + Decimal(request_output) * rates.output_per_mtok
            )
            amount = str((total / _MILLION).quantize(_AMOUNT_PLACES, rounding=ROUND_HALF_EVEN))
        if request_input > 0:
            ratio = float(
                (Decimal(cached_input) / Decimal(request_input)).quantize(_AMOUNT_PLACES)
            )
    expected = _expected_hours(start, end, rows)
    return {
        "currency": rates.currency if rates is not None else COST_CURRENCY,
        "amount": amount,
        "tokens": {
            "uncached_input": str(uncached),
            "cached_input": str(cached_input),
            "output": str(request_output),
        },
        "cache_hit_ratio": ratio,
        "priced_hours": len(priced),
        "expected_hours": expected,
        "partial": len(priced) < expected,
        "flags": flags,
    }


def _range_payload(
    conn: sqlite3.Connection,
    source_id: str,
    series: _Series,
    start: datetime | None,
    now: datetime,
    poll_interval_seconds: int,
    last_success_at: str | None,
    cost_rates: CostRates | None = None,
) -> dict[str, Any]:
    """Sum retained hourly rows through the current observed hour.

    The range ends at the request time; opening balances are never included
    because hourly rows only carry deltas.
    """
    if start is None:
        return {
            "started_at": None,
            "ended_at": to_iso(now),
            "observed": False,
            "counters": {key: _counter_block(None) for key in COUNTER_KEYS},
            "processed_total": None,
            "quality_flags": [],
            "metadata_complete": True,
            "gap_seconds": {},
            "partial": False,
            "incomplete": True,
            "api_equivalent": _api_equivalent({}, now, now, cost_rates),
        }
    label_to = hour_label(now + timedelta(hours=1))
    rows = _hourly_rows(conn, source_id, series.identity, hour_label(start), label_to)
    blocks: dict[str, dict[str, Any]] = {}
    for per_hour in rows.values():
        _merge_counters(blocks, per_hour)
    spans = _scope_spans(conn, source_id, series.identity, start, now)
    implicit = _implicit_downtime(last_success_at, now, poll_interval_seconds)
    gaps = _gap_lookup(spans, implicit, start, now, now)
    payload = _blocks_payload(blocks)
    for key, block in payload["counters"].items():
        block["gap_seconds"] = _gap_seconds_for_counter(gaps, key)
    payload.update(
        {
            "started_at": to_iso(start),
            "ended_at": to_iso(now),
            "observed": bool(blocks),
            "gap_seconds": {scope: gaps[scope] for scope in sorted(gaps)},
            "partial": _is_partial(payload, gaps),
            "incomplete": True,
            "api_equivalent": _api_equivalent(rows, start, now, cost_rates),
        }
    )
    return payload


def _rates(conn: sqlite3.Connection, source_id: str, identity: str) -> dict[str, Any]:
    """Latest accepted interval per counter; null unless the writer deemed it eligible."""
    rows = conn.execute(
        "SELECT counter_key, observed_at, tokens, interval_seconds, eligible,"
        " quality_json FROM latest_interval"
        " WHERE source_id = ? AND identity = ? ORDER BY counter_key",
        (source_id, identity),
    ).fetchall()
    found = {row[0]: row for row in rows}
    rates: dict[str, Any] = {}
    for key in COUNTER_KEYS:
        row = found.get(key)
        if row is None:
            rates[key] = {
                "observed_at": None,
                "tokens": None,
                "interval_seconds": None,
                "tokens_per_second": None,
                "eligible": False,
                "quality_flags": [],
            }
            continue
        tokens, seconds, eligible = row[2], row[3], bool(row[4])
        usable = eligible and tokens is not None and seconds is not None and seconds > 0
        rates[key] = {
            "observed_at": row[1],
            "tokens": str(tokens) if tokens is not None else None,
            "interval_seconds": seconds,
            "tokens_per_second": (
                round(tokens / seconds, 6) if usable else None
            ),
            "eligible": eligible,
            "quality_flags": [flag for flag in QUALITY_FLAGS if flag in _json_list(row[5])],
        }
    return rates


def _epoch_page(
    conn: sqlite3.Connection,
    source_id: str,
    identity: str,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    binding = {"r": "epochs", "i": identity}
    payload = (
        decode_cursor(cursor, route="epochs", binding=binding) if cursor else None
    )
    high_water = (
        conn.execute(
            "SELECT MAX(epoch_id) FROM epochs WHERE source_id = ? AND identity = ?",
            (source_id, identity),
        ).fetchone()[0]
    )
    if payload is None:
        after_time, after_id = "9999-12-31T23:59:59.999999Z", 2**63 - 1
        high_water = high_water if high_water is not None else 0
    else:
        after_time, after_id = payload["t"], payload["k"]
        high_water = payload["hw"]
    rows = conn.execute(
        "SELECT epoch_id, reason, started_at, opening_computed_input,"
        " opening_generated_output FROM epochs"
        " WHERE source_id = ? AND identity = ? AND epoch_id <= ?"
        "   AND (started_at < ? OR (started_at = ? AND epoch_id < ?))"
        " ORDER BY started_at DESC, epoch_id DESC LIMIT ?",
        (source_id, identity, high_water, after_time, after_time, after_id, limit + 1),
    ).fetchall()
    items = [
        {
            "epoch_id": row[0],
            "reason": row[1],
            "started_at": row[2],
            "opening_computed_input": _str_or_none(row[3]),
            "opening_generated_output": _str_or_none(row[4]),
        }
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit and items:
        next_cursor = encode_cursor(
            {
                "r": "epochs",
                "i": identity,
                "hw": high_water,
                "t": items[-1]["started_at"],
                "k": items[-1]["epoch_id"],
            }
        )
    return {"items": items, "next_cursor": next_cursor}


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# public read entry points (one read snapshot each)
# --------------------------------------------------------------------------- #
def _translated(fn):  # noqa: ANN001 - small decorator
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003
        try:
            return fn(*args, **kwargs)
        except QueryError:
            raise
        except OverflowError as exc:
            raise QueryError(
                "counter_overflow",
                "token totals exceed the exact signed 64-bit range",
                500,
            ) from exc
        except (storage.StorageError, sqlite3.Error) as exc:
            raise QueryError("storage_unavailable", "storage is not readable", 503) from exc

    return wrapper


@_translated
def read_status(
    db_path: Path | str,
    *,
    source_id: str,
    poll_interval_seconds: int,
    raw_retention_days: int,
    health: HealthSnapshot,
    now: datetime,
    cost_rates: CostRates | None = None,
) -> dict[str, Any]:
    """Source-level status: no endpoint credentials, URLs, or raw payloads."""
    with _open(db_path) as conn:
        first_success, last_success, last_attempt, last_error = storage.source_activity(
            conn, source_id
        )
        row = conn.execute(
            "SELECT MIN(first_seen_at), MAX(last_seen_at), COUNT(*) FROM series"
            " WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        runtime = {
            key: storage.get_runtime_state(conn, key)
            for key in (
                storage.RUNTIME_LAST_POLL_AT,
                storage.RUNTIME_LAST_ERROR_CODE,
                storage.RUNTIME_STORAGE_STATUS,
                storage.RUNTIME_COLLECTOR_STATUS,
            )
        }
    first_success_at = first_success or row[0]
    return {
        "source_id": source_id,
        "poll_interval_seconds": poll_interval_seconds,
        "raw_retention_days": raw_retention_days,
        "server_time": to_iso(now),
        "first_observation_at": first_success_at,
        "last_attempt_at": last_attempt,
        "last_success_at": last_success,
        "last_error_code": last_error or None,
        "series_count": row[2] or 0,
        "latest_observed_at": row[1],
        "storage_status": health.storage_status,
        "storage_code": health.storage_code,
        "collector_status": health.collector_status,
        "collector_code": health.collector_code,
        "shutting_down": health.shutting_down,
        "last_poll_at": runtime[storage.RUNTIME_LAST_POLL_AT],
        "last_recorded_error_code": runtime[storage.RUNTIME_LAST_ERROR_CODE] or None,
        # Rates appear once, on status; null means the estimate is disabled (D5).
        "cost_rates": None if cost_rates is None else cost_rates.as_wire(),
    }


@_translated
def read_series_page(
    db_path: Path | str,
    *,
    source_id: str,
    limit: int,
    cursor: str | None,
    now: datetime,
    poll_interval_seconds: int,
    health: HealthSnapshot,
) -> dict[str, Any]:
    """Deterministic keyset page over the monotonic series enumeration key."""
    binding = {"r": "series", "s": source_id}
    payload = decode_cursor(cursor, route="series", binding=binding) if cursor else None
    with _open(db_path) as conn:
        high_water = conn.execute(
            "SELECT MAX(seq) FROM series WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
        high_water = high_water if high_water is not None else 0
        if payload is None:
            last_seq, last_identity = -1, ""
        else:
            last_seq, last_identity = payload["k"][0], payload["k"][1]
            high_water = payload["hw"]
        rows = conn.execute(
            "SELECT identity, labels_json, first_seen_at, last_seen_at,"
            " first_valid_at, last_valid_at, stale_since, seq FROM series"
            " WHERE source_id = ? AND seq IS NOT NULL AND seq <= ?"
            "   AND (seq > ? OR (seq = ? AND identity > ?))"
            " ORDER BY seq, identity LIMIT ?",
            (source_id, high_water, last_seq, last_seq, last_identity, limit + 1),
        ).fetchall()
        stale_after = timedelta(seconds=poll_interval_seconds * IMPLICIT_GAP_INTERVALS)
        items = [
            _series_item(source_id, row, now, stale_after) for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit and items:
            next_cursor = encode_cursor(
                {
                    "r": "series",
                    "s": source_id,
                    "hw": high_water,
                    "k": [items[-1]["_seq"], items[-1]["_identity"]],
                }
            )
    for item in items:
        item.pop("_seq", None)
        item.pop("_identity", None)
    return {
        "items": items,
        "next_cursor": next_cursor,
        "page_size": limit,
        "collection_status": health.collector_status,
        "storage_status": health.storage_status,
    }


def _series_item(
    source_id: str, row: tuple[Any, ...], now: datetime, stale_after: timedelta
) -> dict[str, Any]:
    identity, _labels_json, first_seen, last_seen, first_valid, last_valid, stale, seq = row
    labels = labels_from_canonical(identity)
    model_name, engine = series_display(labels)
    stale_since = stale
    if stale_since is None and last_seen:
        # Freshness is derived, never invented: an unobserved series is stale
        # once two poll intervals have passed without an observation.
        if now - parse_iso(last_seen) > stale_after:
            stale_since = to_iso(now - stale_after)
    return {
        "series_id": encode_series_id(source_id, identity),
        "_identity": identity,
        "_seq": seq,
        "labels": labels,
        "model_name": model_name,
        "engine": engine,
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
        "first_valid_at": first_valid,
        "last_valid_at": last_valid,
        "state": "stale" if stale_since else "active",
        "stale_since": stale_since,
    }


@_translated
def read_summary(
    db_path: Path | str,
    *,
    source_id: str,
    series_id: str,
    epoch_limit: int,
    epoch_cursor: str | None,
    health: HealthSnapshot,
    now: datetime,
    poll_interval_seconds: int,
    cost_rates: CostRates | None = None,
) -> dict[str, Any]:
    """Today / month / lifetime sums, latest rates, and bounded epoch diagnostics."""
    limit = min(max(1, epoch_limit), MAX_EPOCH_PAGE)
    if epoch_cursor is not None:
        # Shape/route validation needs no database: a bad cursor is a 400.
        decode_cursor(epoch_cursor, route="epochs", binding={})
    with _open(db_path) as conn:
        series = _resolve(conn, source_id, series_id)
        _first_success, last_success, _attempt, _error = storage.source_activity(
            conn, source_id
        )
        state = conn.execute(
            "SELECT epoch_id, last_accepted_at, stale_since FROM series_state"
            " WHERE source_id = ? AND identity = ?",
            (source_id, series.identity),
        ).fetchone()
        current = None
        if state is not None:
            current = conn.execute(
                "SELECT epoch_id, reason, started_at, opening_computed_input,"
                " opening_generated_output FROM epochs WHERE epoch_id = ?",
                (state[0],),
            ).fetchone()
        today_start = _day_floor(now)
        month_start = today_start.replace(day=1)
        lifetime_start = _first_observation(series)
        epochs = _epoch_page(conn, source_id, series.identity, limit, epoch_cursor)
        payload = {
            "series_id": series.series_id,
            "labels": series.labels,
            "model_name": series.model_name,
            "engine": series.engine,
            "first_observation_at": None
            if lifetime_start is None
            else to_iso(lifetime_start),
            "last_observed_at": series.last_seen_at,
            "last_accepted_at": state[1] if state else None,
            "state": "stale" if (state and state[2]) else "active",
            "ranges": {
                "today": _range_payload(
                    conn,
                    source_id,
                    series,
                    today_start,
                    now,
                    poll_interval_seconds,
                    last_success,
                    cost_rates,
                ),
                "month": _range_payload(
                    conn,
                    source_id,
                    series,
                    month_start,
                    now,
                    poll_interval_seconds,
                    last_success,
                    cost_rates,
                ),
                "lifetime": _range_payload(
                    conn,
                    source_id,
                    series,
                    lifetime_start,
                    now,
                    poll_interval_seconds,
                    last_success,
                    cost_rates,
                ),
            },
            "rates": _rates(conn, source_id, series.identity),
            "epoch": None
            if current is None
            else {
                "epoch_id": current[0],
                "reason": current[1],
                "started_at": current[2],
                "opening_computed_input": _str_or_none(current[3]),
                "opening_generated_output": _str_or_none(current[4]),
            },
            "epochs": epochs,
            "collection_status": health.collector_status,
            "storage_status": health.storage_status,
        }
    return payload


def _first_observation(series: _Series) -> datetime | None:
    for value in (series.first_seen_at, series.first_valid_at):
        if value:
            return parse_iso(value)
    return None


@_translated
def read_history(
    db_path: Path | str,
    *,
    source_id: str,
    series_id: str,
    started_at: datetime,
    ended_at: datetime,
    bucket: str,
    health: HealthSnapshot,
    now: datetime,
    poll_interval_seconds: int,
    cost_rates: CostRates | None = None,
) -> dict[str, Any]:
    """Every bucket in the range, including empty ones; the range is never clamped."""
    seconds = BUCKET_SECONDS[bucket]
    cost_end = min(ended_at, now)
    with _open(db_path) as conn:
        series = _resolve(conn, source_id, series_id)
        _first, last_success, _attempt, _error = storage.source_activity(conn, source_id)
        buckets = _bucket_payloads(
            conn,
            source_id,
            series,
            started_at,
            ended_at,
            seconds,
            now,
            poll_interval_seconds,
            last_success,
        )
        # Cost needs hourly resolution even when the chart is bucketed by day. The
        # label range is half-open like the buckets: a past range stops at
        # `ended_at`; a range reaching past `now` stops after the current hour.
        cost_rows = _hourly_rows(
            conn,
            source_id,
            series.identity,
            hour_label(started_at),
            hour_label(min(ended_at, now + _HOUR)),
        )
    blocks: dict[str, dict[str, Any]] = {}
    for item in buckets:
        for key, block in item["counters"].items():
            merged = blocks.setdefault(key, _counter_block(None))
            if block["observed_intervals"] == 0:
                merged["metadata_complete"] = merged["metadata_complete"] and block[
                    "metadata_complete"
                ]
                continue
            merged["observed_intervals"] += block["observed_intervals"]
            if block["tokens"] is not None:
                merged["tokens"] = str(
                    checked_add_int64(int(merged["tokens"] or 0), int(block["tokens"]))
                )
            merged["first_observed_at"] = _min_iso(
                merged["first_observed_at"], block["first_observed_at"]
            )
            merged["last_observed_at"] = _max_iso(
                merged["last_observed_at"], block["last_observed_at"]
            )
            merged["gap_seconds"] = round(
                merged["gap_seconds"] + block["gap_seconds"], 6
            )
            flags = set(merged["quality_flags"]) | set(block["quality_flags"])
            merged["quality_flags"] = [flag for flag in QUALITY_FLAGS if flag in flags]
            merged["metadata_complete"] = merged["metadata_complete"] and block[
                "metadata_complete"
            ]
    for merged in blocks.values():
        if merged["observed_intervals"] == 0:
            merged["tokens"] = None
    summary = _blocks_payload(blocks)
    gap_totals: dict[str, float] = {}
    for item in buckets:
        for scope, value in item["gap_seconds"].items():
            gap_totals[scope] = round(gap_totals.get(scope, 0.0) + value, 6)
    summary.update(
        {
            "counters": {key: blocks.get(key) or _counter_block(None) for key in COUNTER_KEYS},
            "gap_seconds": {scope: gap_totals[scope] for scope in sorted(gap_totals)},
            "partial": _is_partial(summary, gap_totals)
            or any(item["incomplete"] for item in buckets),
            "incomplete": ended_at > now,
            "started_at": to_iso(started_at),
            "ended_at": to_iso(ended_at),
            "observed": any(
                block["observed_intervals"] > 0
                for item in buckets
                for block in item["counters"].values()
            ),
            "api_equivalent": _api_equivalent(cost_rows, started_at, cost_end, cost_rates),
        }
    )
    return {
        "series_id": series.series_id,
        "model_name": series.model_name,
        "engine": series.engine,
        "bucket": bucket,
        "from": to_iso(started_at),
        "to": to_iso(ended_at),
        "bucket_count": len(buckets),
        "buckets": buckets,
        "range": summary,
        "collection_status": health.collector_status,
        "storage_status": health.storage_status,
    }


@_translated
def read_events(
    db_path: Path | str,
    *,
    source_id: str,
    series_id: str,
    limit: int,
    cursor: str | None,
    now: datetime,
    raw_retention_days: int,
) -> dict[str, Any]:
    """Descending keyset page over series and source-wide events."""
    binding = {"r": "events", "s": source_id, "i": None}
    with _open(db_path) as conn:
        series = _resolve(conn, source_id, series_id)
        binding["i"] = series.identity
        payload = decode_cursor(cursor, route="events", binding=binding) if cursor else None
        high_water = conn.execute(
            "SELECT MAX(event_id) FROM events WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
        high_water = high_water if high_water is not None else 0
        if payload is None:
            after_time, after_id = "9999-12-31T23:59:59.999999Z", 2**63 - 1
        else:
            after_time, after_id, high_water = payload["t"], payload["k"], payload["hw"]
        rows = conn.execute(
            "SELECT event_id, kind, occurred_at, identity, details_json FROM events"
            " WHERE source_id = ? AND (identity = ? OR identity IS NULL)"
            "   AND event_id <= ?"
            "   AND (occurred_at < ? OR (occurred_at = ? AND event_id < ?))"
            " ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
            (
                source_id,
                series.identity,
                high_water,
                after_time,
                after_time,
                after_id,
                limit + 1,
            ),
        ).fetchall()
        raw_until = conn.execute(
            "SELECT MAX(observed_at) FROM samples WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
        raw_from = conn.execute(
            "SELECT MIN(observed_at) FROM samples WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
    items = [
        {
            "event_id": row[0],
            "kind": row[1],
            "occurred_at": row[2],
            "series_id": None if row[3] is None else encode_series_id(source_id, row[3]),
            "details": _json_object(row[4]),
        }
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit and items:
        next_cursor = encode_cursor(
            {
                "r": "events",
                "s": source_id,
                "i": series.identity,
                "t": items[-1]["occurred_at"],
                "k": items[-1]["event_id"],
                "hw": high_water,
            }
        )
    return {
        "series_id": series.series_id,
        "items": items,
        "next_cursor": next_cursor,
        "page_size": limit,
        "retention": {
            "raw_retention_days": raw_retention_days,
            "raw_samples_from": raw_from,
            "raw_samples_until": raw_until,
            "note": (
                f"events and raw samples are retained for {raw_retention_days} days; "
                "hourly totals and gap spans are retained indefinitely"
            ),
        },
    }


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "MAX_EPOCH_PAGE",
    "QueryError",
    "decode_cursor",
    "decode_series_id",
    "encode_cursor",
    "encode_series_id",
    "parse_range",
    "parse_utc",
    "read_events",
    "read_history",
    "read_series_page",
    "read_status",
    "read_summary",
]
