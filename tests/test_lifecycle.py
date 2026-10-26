"""Lifecycle health rules (Delivery 2 Step 3, spec D5/D6).

Distinguishing scenarios: the collection status must keep starting / offline /
degraded / healthy apart, and readiness must depend only on the writer, storage
and shutdown state — never on a Jarvis outage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jarvis_dashboard.lifecycle import (
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_OFFLINE,
    STATUS_STARTING,
    STORAGE_OK,
    STORAGE_UNAVAILABLE,
    RuntimeHealth,
)

T0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
POLL = 15


def health() -> RuntimeHealth:
    record = RuntimeHealth(poll_interval_seconds=POLL)
    record.mark_started(T0)
    return record


def test_no_success_is_starting_then_offline():
    record = health()
    assert record.collection_status(T0 + timedelta(seconds=5)) == STATUS_STARTING
    # Attempts without any success beyond two intervals: offline, not starting.
    record.mark_attempt(T0 + timedelta(seconds=31), "connection_failed")
    assert record.collection_status(T0 + timedelta(seconds=31)) == STATUS_OFFLINE


def test_stale_primary_success_is_offline():
    record = health()
    record.mark_writer_ready()
    record.mark_primary_success(T0)
    assert record.collection_status(T0 + timedelta(seconds=25)) == STATUS_HEALTHY
    assert record.collection_status(T0 + timedelta(seconds=31)) == STATUS_OFFLINE


def test_usable_data_is_healthy():
    record = health()
    record.mark_writer_ready()
    record.mark_primary_success(T0 + timedelta(seconds=15))
    assert record.collection_status(T0 + timedelta(seconds=20)) == STATUS_HEALTHY


def test_degraded_inputs():
    for degrade in ("uptime", "optional", "failure"):
        record = health()
        record.mark_writer_ready()
        record.mark_primary_success(T0 + timedelta(seconds=15))
        if degrade == "uptime":
            record.mark_uptime_available(False)
        elif degrade == "optional":
            record.mark_optional_available(False)
        else:
            record.mark_primary_failure("connection_failed")
        assert record.collection_status(T0 + timedelta(seconds=20)) == STATUS_DEGRADED, degrade
        assert record.snapshot(T0 + timedelta(seconds=20)).ready is True, (
            f"{degrade}: a Jarvis outage must not fail readiness"
        )


def test_primary_success_is_separate_from_transport_success():
    record = health()
    record.mark_writer_ready()
    record.mark_attempt(T0 + timedelta(seconds=15), None)  # bytes arrived, nothing usable
    assert record.collection_status(T0 + timedelta(seconds=16)) == STATUS_STARTING
    record.mark_primary_success(T0 + timedelta(seconds=30))
    assert record.collection_status(T0 + timedelta(seconds=31)) == STATUS_HEALTHY


def test_readiness_requires_writer_storage_and_thread():
    record = health()
    assert record.snapshot(T0).ready is False
    record.mark_writer_ready()
    assert record.snapshot(T0).ready is True
    record.mark_storage_failed("disk_full")
    snapshot = record.snapshot(T0)
    assert snapshot.ready is False
    assert snapshot.storage_status == STORAGE_UNAVAILABLE
    assert snapshot.storage_code == "disk_full"
    record.mark_storage_ok()
    assert record.snapshot(T0).storage_status == STORAGE_OK
    # A storage-ok signal alone never re-arms readiness: only the writer's
    # validated write path does.
    assert record.snapshot(T0).ready is False
    record.mark_writer_ready()
    assert record.snapshot(T0).ready is True
    record.mark_thread_dead()
    assert record.snapshot(T0).ready is False
    record.mark_thread_dead(expected=True)
    assert record.snapshot(T0).ready is True


def test_shutdown_disables_readiness_immediately():
    record = health()
    record.mark_writer_ready()
    record.begin_shutdown()
    snapshot = record.snapshot(T0)
    assert snapshot.ready is False
    assert snapshot.shutting_down is True
    assert snapshot.storage_status == STORAGE_OK  # storage itself is still fine


def test_snapshot_is_a_stable_independent_record():
    record = health()
    record.mark_writer_ready()
    record.mark_primary_success(T0)
    record.mark_attempt(T0 + timedelta(seconds=1), None)
    before = record.snapshot(T0 + timedelta(seconds=2))
    record.mark_storage_failed("database_corrupt")
    assert before.storage_status == STORAGE_OK
    assert before.collector_status == STATUS_HEALTHY
    assert before.last_primary_success_at == T0
    assert record.snapshot(T0 + timedelta(seconds=3)).storage_status == STORAGE_UNAVAILABLE
