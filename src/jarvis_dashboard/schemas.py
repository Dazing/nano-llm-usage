"""Pydantic response models: the frozen wire contract (spec D2/D6).

Every payload carries ``schema_version`` and a per-request UTC ``generated_at``.
Token values are non-negative decimal *strings* so a browser can render them
exactly; counts are JSON integers; durations and rates are numbers with units
named in the field. OpenAPI is generated from these models, so the documented
schema and the served payload cannot drift.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
NO_STORE = "no-store"


class Envelope(BaseModel):
    """Fields present on every response, success and failure alike."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    generated_at: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(Envelope):
    error: ErrorDetail


class LivenessResponse(Envelope):
    status: str


class ReadinessResponse(Envelope):
    status: str
    storage_status: str
    storage_code: str | None = None
    collector_status: str


class ReadinessErrorResponse(ReadinessResponse):
    error: ErrorDetail


class GapScopes(BaseModel):
    """Unobserved seconds per collection scope inside a bucket or range."""

    primary: float
    request_input: float
    request_output: float


class CounterBlock(BaseModel):
    """One counter's coverage for a bucket or range.

    ``tokens`` is null when the counter was not observed at all; a real zero
    total is the string ``"0"`` with a non-zero observed-interval count.
    """

    tokens: str | None = None
    observed_intervals: int
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    gap_seconds: float
    quality_flags: list[str]
    metadata_complete: bool


class CounterBlocks(BaseModel):
    computed_input: CounterBlock
    generated_output: CounterBlock
    request_input: CounterBlock
    request_output: CounterBlock


class Aggregates(BaseModel):
    counters: CounterBlocks
    processed_total: str | None = None
    quality_flags: list[str]
    metadata_complete: bool
    gap_seconds: GapScopes
    partial: bool
    incomplete: bool


class Bucket(Aggregates):
    bucket: str
    started_at: str
    ended_at: str


class Range(Aggregates):
    started_at: str | None = None
    ended_at: str
    observed: bool


class Rate(BaseModel):
    """Latest accepted interval per counter; null when not eligible."""

    observed_at: str | None = None
    tokens: str | None = None
    interval_seconds: float | None = None
    tokens_per_second: float | None = None
    eligible: bool
    quality_flags: list[str]


class Epoch(BaseModel):
    epoch_id: int
    reason: str
    started_at: str
    opening_computed_input: str | None = None
    opening_generated_output: str | None = None


class EpochPage(BaseModel):
    items: list[Epoch]
    next_cursor: str | None = None


class StatusResponse(Envelope):
    source_id: str
    poll_interval_seconds: int
    raw_retention_days: int
    server_time: str
    first_observation_at: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_error_code: str | None = None
    series_count: int
    latest_observed_at: str | None = None
    storage_status: str
    storage_code: str | None = None
    collector_status: str
    collector_code: str | None = None
    shutting_down: bool
    last_poll_at: str | None = None
    last_recorded_error_code: str | None = None


class SeriesItem(BaseModel):
    series_id: str
    labels: dict[str, str]
    model_name: str
    engine: str
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    first_valid_at: str | None = None
    last_valid_at: str | None = None
    state: str
    stale_since: str | None = None


class SeriesListResponse(Envelope):
    items: list[SeriesItem]
    next_cursor: str | None = None
    page_size: int
    collection_status: str
    storage_status: str


class SummaryRanges(BaseModel):
    today: Range
    month: Range
    lifetime: Range


class SummaryResponse(Envelope):
    series_id: str
    labels: dict[str, str]
    model_name: str
    engine: str
    first_observation_at: str | None = None
    last_observed_at: str | None = None
    last_accepted_at: str | None = None
    state: str
    ranges: SummaryRanges
    rates: dict[str, Rate]
    epoch: Epoch | None = None
    epochs: EpochPage
    collection_status: str
    storage_status: str


class HistoryResponse(Envelope):
    series_id: str
    model_name: str
    engine: str
    bucket: str
    from_: str = Field(alias="from")
    to: str
    bucket_count: int
    buckets: list[Bucket]
    range: Range
    collection_status: str
    storage_status: str


class EventItem(BaseModel):
    event_id: int
    kind: str
    occurred_at: str
    series_id: str | None = None
    details: dict[str, object]


class Retention(BaseModel):
    raw_retention_days: int
    raw_samples_from: str | None = None
    raw_samples_until: str | None = None
    note: str


class EventsResponse(Envelope):
    series_id: str
    items: list[EventItem]
    next_cursor: str | None = None
    page_size: int
    retention: Retention
