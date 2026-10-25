"""Shared domain records, series identity, UTC helpers and quality flags.

Pure data only: no HTTP, no SQL, no clock reads. Wall-clock values enter the
domain as explicit observation inputs; scheduling uses injected monotonic
clocks (see ``collector``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self

# Stable counter keys (specification D3).
COMPUTED_INPUT = "computed_input"
GENERATED_OUTPUT = "generated_output"
REQUEST_INPUT = "request_input"
REQUEST_OUTPUT = "request_output"
COUNTER_KEYS: tuple[str, ...] = (COMPUTED_INPUT, GENERATED_OUTPUT, REQUEST_INPUT, REQUEST_OUTPUT)
PRIMARY_KEYS: tuple[str, ...] = (COMPUTED_INPUT, GENERATED_OUTPUT)
OPTIONAL_KEYS: tuple[str, ...] = (REQUEST_INPUT, REQUEST_OUTPUT)

# Gap scopes: the two primary counters share one gap scope; optional counters
# each own their scope (specification D4, delivery plan D3).
PRIMARY_SCOPE = "primary"
SCOPES: tuple[str, ...] = (PRIMARY_SCOPE, REQUEST_INPUT, REQUEST_OUTPUT)

SCOPE_FOR_COUNTER = {
    COMPUTED_INPUT: PRIMARY_SCOPE,
    GENERATED_OUTPUT: PRIMARY_SCOPE,
    REQUEST_INPUT: REQUEST_INPUT,
    REQUEST_OUTPUT: REQUEST_OUTPUT,
}

# Raw metric names as exposed upstream (family names are normalised by some
# parsers, so the raw exposition text is scanned by these exact names).
METRIC_FOR_COUNTER = {
    COMPUTED_INPUT: "radiance:prefill_tokens_total",
    GENERATED_OUTPUT: "radiance:decode_tokens_total",
    REQUEST_INPUT: "vllm:prompt_tokens_total",
    REQUEST_OUTPUT: "vllm:generation_tokens_total",
}
COUNTER_FOR_METRIC = {metric: key for key, metric in METRIC_FOR_COUNTER.items()}

# Upstream values above this are not exactly representable as IEEE-754 doubles
# and are rejected as unsupported (specification D3).
MAX_EXACT_UPSTREAM = 2**53 - 1
# Counters and sums are stored as nonnegative signed 64-bit integers.
INT64_MAX = 2**63 - 1

MAX_SELECTED_IDENTITIES = 256
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_SAMPLES_PER_SCRAPE = 20_000

# /stats process-start evidence tolerance (specification D4 item 4).
UPTIME_EVIDENCE_TOLERANCE = timedelta(seconds=2)


class QualityFlag(StrEnum):
    COLLECTION_GAP = "collection_gap"
    RESET_BOUNDARY = "reset_boundary"
    CONTINUITY_UNVERIFIED = "continuity_unverified"
    TIME_ATTRIBUTION_APPROXIMATE = "time_attribution_approximate"
    CLOCK_DISCONTINUITY = "clock_discontinuity"
    COUNTER_UNAVAILABLE = "counter_unavailable"


class EpochReason(StrEnum):
    NEW_SERIES = "new_series"
    ENDPOINT_CHANGE = "endpoint_change"
    COUNTER_RESET = "counter_reset"
    RESTART_EVIDENCE = "restart_evidence"
    CLOCK_REGRESSION = "clock_regression"


# ---------------------------------------------------------------------------
# Time helpers (UTC only)
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    """Fixed-width sortable UTC ISO-8601 with microseconds."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not accepted; use tz-aware UTC")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hour_floor(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hour_label(value: datetime) -> str:
    return to_iso(hour_floor(value))[:13] + "Z"


def split_span_by_hour(start: datetime, end: datetime) -> list[tuple[datetime, float, datetime, datetime]]:
    """Split [start, end) into per-UTC-hour pieces.

    Returns ``(hour_floor, seconds, piece_start, piece_end)`` with the sum of
    seconds equal to the span length. Empty spans yield an empty list.
    """
    if end <= start:
        return []
    pieces: list[tuple[datetime, float, datetime, datetime]] = []
    cursor = start
    while cursor < end:
        next_hour = hour_floor(cursor) + timedelta(hours=1)
        piece_end = min(next_hour, end)
        pieces.append((hour_floor(cursor), (piece_end - cursor).total_seconds(), cursor, piece_end))
        cursor = piece_end
    return pieces


# ---------------------------------------------------------------------------
# Series identity
# ---------------------------------------------------------------------------


def canonical_labels_json(labels: dict[str, str]) -> str:
    """Deterministic canonical form: keys sorted, compact separators."""
    return json.dumps(labels, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def labels_from_canonical(canonical: str) -> dict[str, str]:
    return json.loads(canonical)


def series_display(labels: dict[str, str]) -> tuple[str | None, str | None]:
    """(model_name, engine) display fields; other labels are preserved, not dropped."""
    return labels.get("model_name"), labels.get("engine")


# ---------------------------------------------------------------------------
# Committed per-series state (mirrors collector_state rows)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrimarySlot:
    """Coupled engine baseline pair (specification D4): updated together."""

    computed_input: int | None = None
    generated_output: int | None = None
    observed_at: datetime | None = None
    continuity_verified: bool = False
    # Intersected process-start interval from /stats uptime evidence.
    uptime_lo: datetime | None = None
    uptime_hi: datetime | None = None


@dataclass(frozen=True)
class OptionalSlot:
    """Independent per-optional-counter baseline and continuity."""

    baseline: int | None = None
    observed_at: datetime | None = None
    continuity_verified: bool = False


@dataclass(frozen=True)
class OpenGap:
    scope: str
    started_at: datetime


@dataclass(frozen=True)
class SeriesState:
    """Committed durable state for one series (None fields = no baseline yet)."""

    epoch_id: int
    epoch_started_at: datetime
    primary: PrimarySlot
    optional: dict[str, OptionalSlot]
    open_gaps: dict[str, OpenGap]  # scope -> open gap
    last_accepted_at: datetime | None = None
    stale_since: datetime | None = None


@dataclass(frozen=True)
class StatsEvidence:
    """One /stats reading with the request timings needed for D4 item 4."""

    model: str
    uptime_s: float
    request_start: datetime
    response_end: datetime

    def process_start_interval(self) -> tuple[datetime, datetime]:
        lo = self.request_start - timedelta(seconds=self.uptime_s) - UPTIME_EVIDENCE_TOLERANCE
        hi = self.response_end - timedelta(seconds=self.uptime_s) + UPTIME_EVIDENCE_TOLERANCE
        return lo, hi

    @staticmethod
    def intersect(
        a: tuple[datetime, datetime] | None, b: tuple[datetime, datetime]
    ) -> tuple[datetime, datetime] | None:
        if a is None:
            return b
        lo = max(a[0], b[0])
        hi = min(a[1], b[1])
        return (lo, hi) if lo <= hi else None


@dataclass(frozen=True)
class ScrapeCounters:
    """Selected counter values for one series identity in one scrape.

    A value is None when that counter is missing/invalid for this scrape;
    ``problems`` records the per-counter failure code (payload-free).
    """

    labels: dict[str, str]
    values: dict[str, int | None]
    problems: dict[str, str]

    def primary_usable(self) -> bool:
        return all(self.values.get(k) is not None for k in PRIMARY_KEYS)


@dataclass
class ScrapeResult:
    """Parsed scrape for one source; keyed by canonical labels JSON."""

    observed_at: datetime
    identities: dict[str, ScrapeCounters]
    # Whole-scrape rejection (oversize, untrustworthy exposition, too many
    # identities): baselines must not change, all scopes gap.
    scrape_error: str | None = None
    missing_metrics: tuple[str, ...] = ()

    def for_labels(self, labels: dict[str, str]) -> ScrapeCounters | None:
        return self.identities.get(canonical_labels_json(labels))


def checked_add_int64(total: int, delta: int) -> int:
    """Add with visible failure instead of wrapping (delivery plan D2)."""
    result = total + delta
    if not 0 <= result <= INT64_MAX:
        raise OverflowError(
            f"aggregate sum {total} + {delta} is outside the nonnegative signed 64-bit range"
        )
    return result


def merge_quality(current: str | None, new: set[str]) -> str:
    """Union quality flags stored as a sorted JSON array."""
    flags = set(json.loads(current)) if current else set()
    flags |= new
    return json.dumps(sorted(flags))
