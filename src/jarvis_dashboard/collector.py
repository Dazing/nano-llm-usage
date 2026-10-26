"""Sequential durable collector: one in-flight poll, monotonic scheduling, no retries."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import storage
from .accounting import SeriesPlan, transition_series
from .config import Config
from .lifecycle import RuntimeHealth
from .domain import (
    REQUEST_INPUT,
    REQUEST_OUTPUT,
    EpochReason,
    ScrapeResult,
    StatsEvidence,
    labels_from_canonical,
    to_iso,
    utc_now,
)
from .metrics import UpstreamError, UpstreamFetcher, parse_scrape

log = logging.getLogger("jarvis_dashboard.collector")


def endpoint_fingerprint(origin: str) -> str:
    return hashlib.sha256(origin.encode("utf-8")).hexdigest()


@dataclass
class PollReport:
    ok: bool
    applied: int
    error_code: str | None = None


class Collector:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        *,
        wall_clock=utc_now,
        monotonic=time.monotonic,
        sleeper=None,
        health: RuntimeHealth | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self._health = health
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._fingerprint = endpoint_fingerprint(config.radiance_base_url)
        self._fetcher = UpstreamFetcher(
            config.radiance_base_url, config.http_timeout_seconds,
            wall_clock=wall_clock, monotonic=monotonic,
        )
        self._stop = threading.Event()
        self._sleep = sleeper if sleeper is not None else self._stop.wait
        self._poll_seq = 0

    # ------------------------------------------------------------------ loop
    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        at = self._wall_clock()
        if self._health is not None:
            self._health.mark_started(at)
            self._health.mark_storage_ok()
        storage.set_runtime_state(self.conn, storage.RUNTIME_STORAGE_STATUS, "ok")
        storage.set_runtime_state(self.conn, storage.RUNTIME_COLLECTOR_STATUS, "starting")
        next_slot = self._monotonic()
        while not self._stop.is_set():
            self._poll_seq += 1
            poll_id = f"{to_iso(self._wall_clock())}#{self._poll_seq}"
            storage_failed = False
            try:
                self.poll_once(poll_id)
            except storage.StorageError as exc:
                self._mark_unhealthy(exc.code)
                storage_failed = True
            except sqlite3.Error as exc:
                self._mark_unhealthy("storage_error")
                storage_failed = True
                log.warning("storage failure", extra={"error_code": "storage_error", "err": str(exc)})
            self._maintenance()
            if not storage_failed:
                # A failed cycle must not be papered over with a fresh "running".
                storage.set_runtime_state(self.conn, storage.RUNTIME_COLLECTOR_STATUS, "running")
            # Monotonic schedule: catch up by skipping missed slots, never burst.
            next_slot += self.config.poll_interval_seconds
            now = self._monotonic()
            if next_slot <= now:
                missed = int((now - next_slot) // self.config.poll_interval_seconds) + 1
                next_slot += missed * self.config.poll_interval_seconds
                log.info("skipped %d missed poll slot(s)", missed)
            delay = max(0.0, next_slot - self._monotonic())
            self._sleep(delay)

    def _mark_unhealthy(self, code: str) -> None:
        # Publish readiness first: the durable write may be the thing that failed.
        if self._health is not None:
            self._health.mark_storage_failed(code)
        try:
            storage.set_runtime_state(self.conn, storage.RUNTIME_STORAGE_STATUS, "unhealthy")
            storage.set_runtime_state(self.conn, storage.RUNTIME_LAST_ERROR_CODE, code)
        except sqlite3.Error:
            pass  # storage is unhealthy; the in-memory collector keeps trying
        log.warning("collector unhealthy", extra={"error_code": code})

    # ------------------------------------------------------------------ poll
    def poll_once(self, poll_id: str) -> PollReport:
        observed_at = self._wall_clock()
        stats: StatsEvidence | None = None
        try:
            stats = self._to_evidence(self._fetcher.fetch_stats())
            if self._health is not None:
                self._health.mark_uptime_available(True)
        except UpstreamError as exc:
            # D1-AC: engine continuity becomes unverified; deltas are still accepted.
            if self._health is not None:
                self._health.mark_uptime_available(False)
            log.info("stats fetch failed: %s", exc.code)
        try:
            body = self._fetcher.fetch_metrics_text()
        except UpstreamError as exc:
            at = self._wall_clock()
            storage.record_source_attempt(
                self.conn, self.config.source_id, self._fingerprint, at, exc.code
            )
            storage.set_runtime_state(self.conn, storage.RUNTIME_LAST_POLL_AT, to_iso(at))
            storage.set_runtime_state(self.conn, storage.RUNTIME_LAST_ERROR_CODE, exc.code)
            self._publish_failure(at, exc.code)
            log.warning("metrics fetch failed", extra={"error_code": exc.code})
            return PollReport(ok=False, applied=0, error_code=exc.code)
        scrape: ScrapeResult = parse_scrape(body, observed_at)
        if scrape.scrape_error:
            at = self._wall_clock()
            storage.record_source_attempt(
                self.conn, self.config.source_id, self._fingerprint, at, scrape.scrape_error
            )
            self._publish_failure(at, scrape.scrape_error)
            log.warning("metrics parse failed", extra={"error_code": scrape.scrape_error})
            return PollReport(ok=False, applied=0, error_code=scrape.scrape_error)

        stored_fp = storage.stored_endpoint_fingerprint(self.conn, self.config.source_id)
        hint = (
            EpochReason.ENDPOINT_CHANGE
            if stored_fp is not None and stored_fp != self._fingerprint
            else None
        )
        loaded = storage.load_series(self.conn, self.config.source_id)
        plans: list[SeriesPlan] = []
        for identity, counters in scrape.identities.items():
            existing = loaded.get(identity)
            plan = transition_series(
                existing.state if existing else None,
                counters,
                observed_at,
                stats,
                self.config.poll_interval_seconds,
                new_epoch_hint=hint,
            )
            plans.append(plan)
        for identity, series in loaded.items():
            if identity not in scrape.identities:
                plans.append(
                    transition_series(
                        series.state, None, observed_at, stats,
                        self.config.poll_interval_seconds, new_epoch_hint=hint,
                        labels=series.labels,
                    )
                )
        applied = storage.apply_poll(
            self.conn,
            source_id=self.config.source_id,
            endpoint_fingerprint=self._fingerprint,
            poll_id=poll_id,
            observed_at=observed_at,
            plans=plans,
        )
        primary_ok = any(counters.primary_usable() for counters in scrape.identities.values())
        optional_ok = all(
            counters.values.get(REQUEST_INPUT) is not None
            and counters.values.get(REQUEST_OUTPUT) is not None
            for counters in scrape.identities.values()
        )
        storage.record_source_attempt(
            self.conn,
            self.config.source_id,
            self._fingerprint,
            observed_at,
            "",
            primary_success=primary_ok,
        )
        storage.set_runtime_state(self.conn, storage.RUNTIME_LAST_POLL_AT, to_iso(observed_at))
        storage.set_runtime_state(self.conn, storage.RUNTIME_LAST_ERROR_CODE, "")
        storage.set_runtime_state(self.conn, storage.RUNTIME_STORAGE_STATUS, "ok")
        if self._health is not None:
            # Readiness is (re)published only after successful storage work.
            self._health.mark_writer_ready()
            self._health.mark_attempt(observed_at, None)
            self._health.mark_optional_available(optional_ok)
            if primary_ok:
                # Primary success is separate from transport success (spec D5).
                self._health.mark_primary_success(observed_at)
            else:
                self._health.mark_primary_failure("primary_counters_unavailable")
        return PollReport(ok=True, applied=applied)

    def _publish_failure(self, at: datetime, code: str) -> None:
        if self._health is None:
            return
        self._health.mark_attempt(at, code)
        self._health.mark_primary_failure(code)

    @staticmethod
    def _to_evidence(fetched) -> StatsEvidence:
        return StatsEvidence(
            model=fetched.model,
            uptime_s=fetched.uptime_s,
            request_start=fetched.request_start,
            response_end=fetched.response_end,
        )

    # ----------------------------------------------------------- maintenance
    def _maintenance(self) -> None:
        try:
            remaining = storage.drain_gap_materialization(self.conn)
            if remaining:
                log.info("gap materialization resumed, %d pieces queued", remaining)
            last = storage.get_runtime_state(self.conn, storage.LAST_RETENTION_KEY)
            now = self._wall_clock()
            if last is None or now - datetime.fromisoformat(last.replace("Z", "+00:00")) >= timedelta(days=1):
                cutoff = now - timedelta(days=self.config.raw_retention_days)
                removed = storage.purge_expired(self.conn, to_iso(cutoff))
                storage.set_runtime_state(self.conn, storage.LAST_RETENTION_KEY, to_iso(now))
                if removed:
                    log.info("retention removed %d raw rows", removed)
        except sqlite3.Error as exc:
            self._mark_unhealthy("maintenance_error")
            log.warning("maintenance failure", extra={"error_code": "maintenance_error", "err": str(exc)})
