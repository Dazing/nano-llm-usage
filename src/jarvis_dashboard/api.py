"""Bounded ``/api/v1`` routes (spec D3/D5/D6).

Every route answers from one read-only SQLite snapshot inside a single read
transaction, dispatched to a worker thread behind a non-waiting admission gate:
at most ``READ_CAPACITY`` database reads run at once and the next request is
refused immediately with ``read_capacity_exceeded`` instead of queueing. Health
never touches this path, so a saturated API cannot mask liveness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from . import queries
from .domain import to_iso, utc_now
from .queries import QueryError
from .schemas import (
    ErrorResponse,
    EventsResponse,
    HistoryResponse,
    LiveResponse,
    SeriesListResponse,
    StatusResponse,
    SummaryResponse,
)

READ_CAPACITY = 4
# The documented refusal codes are part of the frozen contract; the frontend
# relies on them, so they are advertised in the OpenAPI document.
ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid parameters, range, or cursor."},
    404: {"model": ErrorResponse, "description": "Unknown series."},
    500: {"model": ErrorResponse, "description": "Unexpected failure; no upstream detail."},
    503: {
        "model": ErrorResponse,
        "description": "Storage unavailable, not migrated, or read capacity exhausted.",
    },
}
MAX_EVENTS_PAGE = 100
MAX_SERIES_PAGE = 100

# Served when the app has no live monitor (in-process test app, `collect`), so
# the panel renders its placeholders instead of failing (plan D5).
UNAVAILABLE_LIVE: dict[str, Any] = {
    "state": "unavailable",
    "observed_at": None,
    "requests": {"active": None, "queued": None},
    "prefill_tokens_per_second": None,
    "decode_tokens_per_second": None,
    "decode_tokens_per_second_per_request": None,
    "mtp_acceptance": {
        "window_5s": None,
        "window_1m": None,
        "window_30m": None,
        "lifetime": None,
    },
    "moe_gpu_hit": {"window_5s": None, "window_1m": None, "window_30m": None},
}
LIVE_ERROR_RESPONSES = {
    500: {"model": ErrorResponse, "description": "Unexpected failure; no upstream detail."},
}

log = logging.getLogger("jarvis_dashboard.api")


class ApiFailure(Exception):
    """A refusal with a stable wire code, raised from the route layer."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class ReadAdmission:
    """Non-waiting gate over ``limit`` concurrent database reads.

    Acquire and release run on the event-loop thread with no await between the
    check and the increment, so the count is exact without a lock.
    """

    limit: int = READ_CAPACITY
    in_use: int = 0

    def try_acquire(self) -> bool:
        if self.in_use >= self.limit:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        if self.in_use > 0:
            self.in_use -= 1


def _state(app_state: Any):
    return app_state.dashboard


async def _read(state, work, **params: Any) -> dict[str, Any]:
    """Run one snapshot read behind the admission gate."""
    gate = state.admission
    if not gate.try_acquire():
        raise ApiFailure(
            503,
            "read_capacity_exceeded",
            f"dashboard read capacity ({gate.limit}) is in use; retry shortly",
        )
    try:
        return await run_in_threadpool(work, **params)
    except QueryError as exc:
        raise ApiFailure(exc.status, exc.code, exc.message) from exc
    finally:
        # Released only after the offloaded work is finished, including the
        # cancellation path, so the slot can never leak.
        gate.release()


def _now() -> datetime:
    return utc_now()


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["dashboard"])

    @router.get("/live", response_model=LiveResponse, responses=LIVE_ERROR_RESPONSES, tags=["live"])
    async def live(request: Request) -> dict[str, Any]:
        """Latest 1 Hz snapshot, read straight from memory (plan D5).

        No SQLite read and no admission gate: a saturated read capacity cannot
        make the live row fail, and the live row cannot consume read capacity.
        """
        monitor = _state(request.app.state).live
        payload = UNAVAILABLE_LIVE if monitor is None else monitor.snapshot()
        return _envelope(payload, _now())

    @router.get("/status", response_model=StatusResponse, responses=ERROR_RESPONSES, tags=["dashboard"])
    async def status(request: Request) -> dict[str, Any]:
        """Source state and collection health; never endpoint credentials."""
        state = _state(request.app.state)
        now = _now()
        payload = await _read(
            state,
            queries.read_status,
            db_path=state.db_path,
            source_id=state.config.source_id,
            poll_interval_seconds=state.config.poll_interval_seconds,
            raw_retention_days=state.config.raw_retention_days,
            health=state.health.snapshot(now),
            now=now,
            cost_rates=state.config.cost_rates,
        )
        return _envelope(payload, now)

    @router.get("/series", response_model=SeriesListResponse, responses=ERROR_RESPONSES, tags=["dashboard"])
    async def series(
        request: Request,
        limit: int = Query(100, ge=1, le=MAX_SERIES_PAGE),
        cursor: str | None = Query(None, max_length=queries.MAX_CURSOR_CHARS),
    ) -> dict[str, Any]:
        """Deterministic keyset page of series (default 100, maximum 100)."""
        state = _state(request.app.state)
        now = _now()
        payload = await _read(
            state,
            queries.read_series_page,
            db_path=state.db_path,
            source_id=state.config.source_id,
            limit=limit,
            cursor=cursor,
            now=now,
            poll_interval_seconds=state.config.poll_interval_seconds,
            health=state.health.snapshot(now),
        )
        return _envelope(payload, now)

    @router.get("/summary", response_model=SummaryResponse, responses=ERROR_RESPONSES, tags=["dashboard"])
    async def summary(
        request: Request,
        series_id: str = Query(..., min_length=4, max_length=1024),
        epoch_limit: int = Query(20, ge=1, le=queries.MAX_EPOCH_PAGE),
        epoch_cursor: str | None = Query(None, max_length=queries.MAX_CURSOR_CHARS),
    ) -> dict[str, Any]:
        """Today / month / lifetime totals, latest rates, epoch diagnostics."""
        state = _state(request.app.state)
        now = _now()
        payload = await _read(
            state,
            queries.read_summary,
            db_path=state.db_path,
            source_id=state.config.source_id,
            series_id=series_id,
            epoch_limit=epoch_limit,
            epoch_cursor=epoch_cursor,
            health=state.health.snapshot(now),
            now=now,
            poll_interval_seconds=state.config.poll_interval_seconds,
            cost_rates=state.config.cost_rates,
        )
        return _envelope(payload, now)

    @router.get("/history", response_model=HistoryResponse, responses=ERROR_RESPONSES, tags=["dashboard"])
    async def history(
        request: Request,
        series_id: str = Query(..., min_length=4, max_length=1024),
        from_: str = Query(..., alias="from", min_length=10, max_length=40),
        to: str = Query(..., min_length=10, max_length=40),
        bucket: str = Query(..., min_length=3, max_length=8),
    ) -> dict[str, Any]:
        """Every bucket in a UTC half-open range; ranges are never clamped."""
        state = _state(request.app.state)
        now = _now()
        try:
            started_at, ended_at, _ = queries.parse_range(from_, to, bucket)
        except QueryError as exc:
            raise ApiFailure(exc.status, exc.code, exc.message) from exc
        payload = await _read(
            state,
            queries.read_history,
            db_path=state.db_path,
            source_id=state.config.source_id,
            series_id=series_id,
            started_at=started_at,
            ended_at=ended_at,
            bucket=bucket,
            health=state.health.snapshot(now),
            now=now,
            poll_interval_seconds=state.config.poll_interval_seconds,
            cost_rates=state.config.cost_rates,
        )
        return _envelope(payload, now)

    @router.get("/events", response_model=EventsResponse, responses=ERROR_RESPONSES, tags=["dashboard"])
    async def events(
        request: Request,
        series_id: str = Query(..., min_length=4, max_length=1024),
        limit: int = Query(20, ge=1, le=MAX_EVENTS_PAGE),
        cursor: str | None = Query(None, max_length=queries.MAX_CURSOR_CHARS),
    ) -> dict[str, Any]:
        """Newest-first event page with the raw-retention disclosure."""
        state = _state(request.app.state)
        now = _now()
        payload = await _read(
            state,
            queries.read_events,
            db_path=state.db_path,
            source_id=state.config.source_id,
            series_id=series_id,
            limit=limit,
            cursor=cursor,
            now=now,
            raw_retention_days=state.config.raw_retention_days,
        )
        return _envelope(payload, now)

    return router


def _envelope(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {"schema_version": 1, "generated_at": to_iso(now), **payload}
