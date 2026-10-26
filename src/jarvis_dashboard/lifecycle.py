"""In-memory lifecycle state: readiness and collection health (Delivery 2 Step 3).

Health answers come from this small thread-safe record, never from a fifth
database connection (plan Step 3). The writer thread publishes into it; the
HTTP thread reads it. Storage health and collection health are orthogonal:
only storage (and lifecycle) failures can make readiness fail, so a Jarvis
outage keeps ``/health/ready`` at 200 while the collection status degrades.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from .domain import utc_now

STATUS_STARTING = "starting"
STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_OFFLINE = "offline"

STORAGE_OK = "ok"
STORAGE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class HealthSnapshot:
    """Immutable view of lifecycle state for one request."""

    ready: bool
    shutting_down: bool
    storage_status: str
    storage_code: str | None
    collector_status: str
    collector_code: str | None
    started_at: datetime | None
    last_attempt_at: datetime | None
    last_primary_success_at: datetime | None
    last_error_code: str | None
    first_observation_at: datetime | None


class RuntimeHealth:
    """Thread-safe readiness/collection record owned by the service process."""

    def __init__(self, *, poll_interval_seconds: int) -> None:
        # Re-entrant: snapshot() derives the collection status under the same lock.
        self._lock = threading.RLock()
        self._poll = poll_interval_seconds
        self._started_at: datetime | None = None
        self._writer_ready = False
        self._storage_status = STATUS_STARTING
        self._storage_code: str | None = None
        self._collector_code: str | None = None
        self._last_error_code: str | None = None
        self._last_attempt_at: datetime | None = None
        self._last_primary_success_at: datetime | None = None
        self._first_observation_at: datetime | None = None
        self._primary_failure: str | None = None
        self._uptime_available = True
        self._optional_available = True
        self._shutting_down = False
        self._thread_alive = True

    # -- lifecycle publishing ------------------------------------------------

    def mark_started(self, at: datetime) -> None:
        with self._lock:
            self._started_at = at

    def mark_writer_ready(self) -> None:
        """Storage is open, migrated, provably writable, and collection began."""
        with self._lock:
            self._writer_ready = True
            self._storage_status = STORAGE_OK

    def mark_storage_ok(self) -> None:
        with self._lock:
            self._storage_status = STORAGE_OK
            self._storage_code = None

    def mark_storage_failed(self, code: str) -> None:
        with self._lock:
            self._storage_status = STORAGE_UNAVAILABLE
            self._storage_code = code
            self._writer_ready = False

    def mark_attempt(self, at: datetime, error_code: str | None) -> None:
        """One transport attempt against Jarvis (success here is not primary success)."""
        with self._lock:
            self._last_attempt_at = at
            self._last_error_code = error_code
            self._collector_code = error_code

    def mark_primary_success(self, at: datetime) -> None:
        """The primary counters were accepted into storage (spec D5 status rule)."""
        with self._lock:
            self._last_primary_success_at = at
            self._first_observation_at = self._first_observation_at or at
            self._primary_failure = None
            self._collector_code = None

    def mark_primary_failure(self, code: str) -> None:
        with self._lock:
            self._primary_failure = code
            self._collector_code = code

    def mark_uptime_available(self, available: bool) -> None:
        with self._lock:
            self._uptime_available = available

    def mark_optional_available(self, available: bool) -> None:
        with self._lock:
            self._optional_available = available

    def begin_shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
            self._writer_ready = False

    def mark_thread_dead(self, *, expected: bool = False) -> None:
        with self._lock:
            self._thread_alive = expected

    # -- reads ---------------------------------------------------------------

    def collection_status(self, now: datetime | None = None) -> str:
        """Spec D5 precedence: starting → offline → degraded → healthy."""
        now = now or utc_now()
        with self._lock:
            limit = timedelta(seconds=2 * self._poll)
            if self._last_primary_success_at is None:
                if self._started_at is None or now - self._started_at <= limit:
                    return STATUS_STARTING
                return STATUS_OFFLINE
            if now - self._last_primary_success_at > limit:
                return STATUS_OFFLINE
            if (
                self._primary_failure is not None
                or not self._uptime_available
                or not self._optional_available
            ):
                return STATUS_DEGRADED
            return STATUS_HEALTHY

    def snapshot(self, now: datetime | None = None) -> HealthSnapshot:
        now = now or utc_now()
        with self._lock:
            ready = (
                self._writer_ready
                and self._storage_status == STORAGE_OK
                and self._thread_alive
                and not self._shutting_down
            )
            return HealthSnapshot(
                ready=ready,
                shutting_down=self._shutting_down,
                storage_status=self._storage_status,
                storage_code=self._storage_code,
                collector_status=self.collection_status(now),
                collector_code=self._collector_code,
                started_at=self._started_at,
                last_attempt_at=self._last_attempt_at,
                last_primary_success_at=self._last_primary_success_at,
                last_error_code=self._last_error_code,
                first_observation_at=self._first_observation_at,
            )
