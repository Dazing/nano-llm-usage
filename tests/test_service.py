"""Service lifecycle: writer-thread ownership, refusal, and shutdown (Step 3).

Distinguishing scenarios: the writable connection must be created and used off
the main thread, a second instance must refuse immediately, shutdown must be
prompt and release the volume, an unexpected writer death must fail readiness
and exit nonzero, and a failed startup must never bind a port.
"""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from jarvis_dashboard import service as service_mod
from jarvis_dashboard import storage
from jarvis_dashboard.config import Config
from jarvis_dashboard.storage import StorageError, open_storage

PORT = 46080


class StubCollector:
    """Stands in for the real loop: no network, stops on request."""

    instances: list["StubCollector"] = []

    def __init__(self, config, conn, health):  # noqa: ANN001 - factory signature
        self.config = config
        self.conn = conn
        self.health = health
        self.stop = threading.Event()
        self.ran = threading.Event()
        StubCollector.instances.append(self)

    def run_forever(self) -> None:
        self.ran.set()
        self.stop.wait(30)

    def request_stop(self) -> None:
        self.stop.set()


def make_config(tmp_path: Path, **overrides: object) -> Config:
    values: dict[str, object] = {
        "radiance_base_url": "http://127.0.0.1:8000",
        "source_id": "s1",
        "poll_interval_seconds": 15,
        "http_timeout_seconds": 3.0,
        "database_path": str(tmp_path / "d.sqlite3"),
        "raw_retention_days": 30,
        "port": PORT,
        "log_level": "INFO",
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


def make_service(tmp_path: Path, **overrides: object) -> service_mod.Service:
    config = make_config(tmp_path, **overrides)
    return service_mod.Service(config, collector_factory=StubCollector)


@pytest.fixture(autouse=True)
def _clear_instances():
    StubCollector.instances.clear()
    yield
    StubCollector.instances.clear()


def running_service(tmp_path: Path, **overrides: object) -> service_mod.Service:
    service = make_service(tmp_path, **overrides)
    service.start()
    assert service.wait_started(10)
    return service


def test_writer_thread_owns_the_writable_connection(tmp_path, monkeypatch):
    seen: dict[str, str] = {}
    real_open = storage.open_storage

    def spy(db_path):  # noqa: ANN001 - mirrors storage.open_storage
        seen["thread"] = threading.current_thread().name
        return real_open(db_path)

    monkeypatch.setattr(service_mod, "open_storage", spy)
    service = running_service(tmp_path)
    try:
        assert seen["thread"] == "jarvis-writer"
        assert seen["thread"] != threading.main_thread().name
        snapshot = service.health.snapshot()
        assert snapshot.ready is True
        assert StubCollector.instances[0].conn is not None
    finally:
        service.request_stop()
        assert service.join(12) == 0


def test_readiness_waits_for_real_writability(tmp_path, monkeypatch):
    calls: list[str] = []
    real_validate = service_mod.validate_writable

    def spy(conn):  # noqa: ANN001
        calls.append(threading.current_thread().name)
        return real_validate(conn)

    monkeypatch.setattr(service_mod, "validate_writable", spy)
    service = running_service(tmp_path)
    try:
        assert calls == ["jarvis-writer"]
    finally:
        service.request_stop()
        service.join(12)


def test_second_instance_refuses_instead_of_waiting(tmp_path):
    first = running_service(tmp_path)
    try:
        second = make_service(tmp_path)
        second.start()
        assert second.wait_started(10)
        assert isinstance(second.startup_error, StorageError)
        assert second.startup_error.code == "volume_locked"
        assert second.exit_code == 3
        snapshot = second.health.snapshot()
        assert snapshot.ready is False
        assert snapshot.storage_code == "volume_locked"
    finally:
        first.request_stop()
        first.join(12)


def test_shutdown_is_prompt_and_frees_the_volume(tmp_path):
    service = running_service(tmp_path)
    began = time.monotonic()
    service.request_stop()
    code = service.join(12)
    elapsed = time.monotonic() - began
    assert code == 0
    assert elapsed < 15, f"shutdown took {elapsed:.2f}s"
    assert service.health.snapshot().ready is False
    # The volume is reusable: a new instance can acquire the lock and start.
    successor = running_service(tmp_path)
    successor.request_stop()
    assert successor.join(12) == 0


def test_unexpected_writer_death_fails_readiness_and_exits_nonzero(tmp_path):
    def broken(config, conn, health):  # noqa: ANN001
        raise RuntimeError("collector exploded")

    config = make_config(tmp_path)
    service = service_mod.Service(config, collector_factory=broken)
    service.start()
    assert service.wait_started(10)
    code = service.join(12)
    assert code == 1
    snapshot = service.health.snapshot()
    assert snapshot.ready is False
    assert snapshot.storage_status == "unavailable"


def test_failed_startup_never_binds_the_port(tmp_path, monkeypatch):
    def locked(db_path):  # noqa: ANN001 - mirrors acquire_volume_lock
        raise StorageError("volume_locked", "held elsewhere")

    monkeypatch.setattr(service_mod, "acquire_volume_lock", locked)
    config = make_config(tmp_path, port=46090)
    assert service_mod.run_serve(config) == 3
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", 46090)) != 0


def test_read_only_storage_is_reported_as_a_storage_failure(tmp_path):
    db = tmp_path / "d.sqlite3"
    conn = open_storage(db)
    conn.close()
    db.chmod(0o444)
    (tmp_path / "d.sqlite3.lock").write_text("")
    (tmp_path / "d.sqlite3.lock").chmod(0o444)
    config = make_config(tmp_path, database_path=str(db))
    service = service_mod.Service(config, collector_factory=StubCollector)
    service.start()
    assert service.wait_started(10)
    assert isinstance(service.startup_error, StorageError)
    assert service.exit_code == 3
    assert service.health.snapshot().storage_code in {
        "storage_read_only",
        "storage_unwritable",
        "volume_locked",
    }
    service.join(12)


def test_collect_command_returns_the_writer_exit_code(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service_mod, "_install_plain_signal_handlers", lambda svc: None)

    def stop_after_start(svc: service_mod.Service) -> None:
        StubCollector.instances[0].request_stop()

    monkeypatch.setattr(
        service_mod.Service,
        "wait_finished",
        lambda self, timeout=None: (stop_after_start(self), True)[1],
    )
    assert service_mod.run_collect(make_config(tmp_path), service=service) == 0


def test_validate_writable_reports_read_only_databases(tmp_path):
    db = tmp_path / "w.sqlite3"
    conn = open_storage(db)
    conn.close()
    db.chmod(0o444)
    ro = sqlite3.connect(str(db), isolation_level=None)
    with pytest.raises(StorageError) as raised:
        service_mod.validate_writable(ro)
    assert raised.value.code == "storage_read_only"
    ro.close()
