"""Service lifecycle: one writer thread owns the volume; HTTP serves in the main thread.

The writer thread acquires the volume lock, opens and migrates storage, proves
real transactional writability, then runs the collection loop. It publishes
readiness into :class:`~jarvis_dashboard.lifecycle.RuntimeHealth`, so health
answers never need a database connection. Shutdown order is fixed (plan Step 3):
disable readiness → stop scheduling → request collector stop → let the HTTP
layer drain → close the connection → join the thread, all inside the 15-second
budget.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import signal
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import uvicorn

from .app import AppState, create_asgi_app
from .collector import Collector
from .config import Config
from .domain import utc_now
from .lifecycle import RuntimeHealth
from .storage import StorageError, acquire_volume_lock, open_storage

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_STORAGE = 3

STARTUP_TIMEOUT_SECONDS = 20.0
SHUTDOWN_BUDGET_SECONDS = 12.0
GRACEFUL_HTTP_TIMEOUT_SECONDS = 5.0

log = logging.getLogger("jarvis_dashboard.service")


def write_failure_code(exc: BaseException) -> str:
    """Map a write failure to a stable, actionable code (plan Step 3)."""
    text = str(exc).lower()
    if "readonly" in text or "read-only" in text:
        return "storage_read_only"
    if "locked" in text:
        return "volume_locked"
    if "disk i/o" in text or "full" in text or "no space" in text:
        return "storage_full"
    if "malformed" in text:
        return "database_corrupt"
    return "storage_unwritable"


WRITE_PROBE_KEY = "__write_probe__"


def os_failure_code(exc: OSError) -> str:
    """Map an I/O failure on the volume to a stable code (plan Step 3)."""
    if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
        return "storage_read_only"
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return "storage_full"
    if exc.errno == errno.EBUSY:
        return "volume_locked"
    return "storage_unwritable"


def validate_writable(conn: sqlite3.Connection) -> None:
    """Prove the real write path works before publishing readiness.

    Locking alone is not enough: a WAL database locked in a writable directory
    accepts BEGIN IMMEDIATE even when its main file is read-only, so the probe
    writes a row and rolls the transaction back.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO collector_state (key, value) VALUES (?, ?)",
            (WRITE_PROBE_KEY, "1"),
        )
        conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise StorageError(write_failure_code(exc), str(exc)) from exc


class Service:
    """Owns the single writer thread: volume lock, connection, collector loop."""

    def __init__(
        self,
        config: Config,
        *,
        health: RuntimeHealth | None = None,
        wall_clock: Callable[[], object] = utc_now,
        collector_factory: Callable[[Config, sqlite3.Connection, RuntimeHealth], Collector]
        | None = None,
    ) -> None:
        self.config = config
        self.health = health or RuntimeHealth(
            poll_interval_seconds=config.poll_interval_seconds
        )
        self._wall_clock = wall_clock
        self._collector_factory = collector_factory
        self._thread: threading.Thread | None = None
        self._collector: Collector | None = None
        self._conn: sqlite3.Connection | None = None
        self._lock = None
        self._started = threading.Event()
        self._finished = threading.Event()
        self.exit_code = EXIT_OK
        self.startup_error: StorageError | Exception | None = None

    # -- control -------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_writer, name="jarvis-writer", daemon=True
        )
        self._thread.start()

    def wait_started(self, timeout: float | None = STARTUP_TIMEOUT_SECONDS) -> bool:
        """True once the writer published readiness or failed at startup."""
        return self._started.wait(timeout)

    def request_stop(self) -> None:
        # Readiness goes first: no new requests should be admitted to a dying service.
        self.health.begin_shutdown()
        collector = self._collector
        if collector is not None:
            collector.request_stop()

    def writer_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait_finished(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def join(self, timeout: float | None = SHUTDOWN_BUDGET_SECONDS) -> int:
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.error("writer thread exceeded the shutdown budget")
        return self.exit_code

    # -- writer thread -------------------------------------------------------

    def _run_writer(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            db_path = Path(self.config.database_path)
            self._lock = acquire_volume_lock(db_path)
            conn = open_storage(db_path)
            validate_writable(conn)
            self._conn = conn
            if self._collector_factory is not None:
                collector = self._collector_factory(
                    self.config, conn, self.health
                )
            else:
                collector = Collector(
                    self.config,
                    conn,
                    wall_clock=self._wall_clock,  # type: ignore[arg-type]
                    health=self.health,
                )
            self._collector = collector
            self.health.mark_writer_ready()
            self._started.set()
            collector.run_forever()
            self.health.mark_thread_dead(expected=True)
        except StorageError as exc:
            # Publish before any durable logging: the durable write may be broken.
            self.startup_error = exc
            self.exit_code = EXIT_STORAGE
            self.health.mark_storage_failed(exc.code)
            log.error("storage failure: %s", exc.code)
        except OSError as exc:
            # The volume itself is unusable (read-only mount, quota, permissions).
            self.startup_error = StorageError(os_failure_code(exc), str(exc))
            self.exit_code = EXIT_STORAGE
            self.health.mark_storage_failed(os_failure_code(exc))
            log.error("volume failure: %s", os_failure_code(exc))
        except BaseException as exc:  # noqa: BLE001 - unexpected writer death
            self.startup_error = exc
            self.exit_code = EXIT_RUNTIME
            self.health.mark_storage_failed(write_failure_code(exc))
            self.health.mark_thread_dead()
            log.error("writer thread stopped: %s", type(exc).__name__)
        finally:
            self._started.set()  # unblock startup waiters; check startup_error
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.commit()  # finish the active transaction, never partial
                    self._conn.close()
                self._conn = None
            if self._lock is not None:
                self._lock.release()
                self._lock = None
            self._finished.set()


def _bind_host(config: Config) -> str:
    # Explicit IP literal from validated config; never an implicit wildcard (spec D8).
    return config.bind_address


class _ServingServer(uvicorn.Server):
    """Uvicorn server whose shutdown stops the service, without signal re-raise."""

    def __init__(self, config: uvicorn.Config, service: Service) -> None:
        super().__init__(config)
        self._service = service

    def handle_exit(self, sig: int, frame: object) -> None:
        self._service.request_stop()
        super().handle_exit(sig, frame)

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[no-untyped-def]
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {
            sig: signal.signal(sig, self.handle_exit) for sig in _HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)


try:  # pragma: no cover - platform dependent
    _HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)
except AttributeError:  # pragma: no cover - Windows has no SIGTERM
    _HANDLED_SIGNALS = (signal.SIGINT,)


def build_server(config: Config, service: Service) -> _ServingServer:
    state = AppState(
        config=config,
        health=service.health,
        db_path=Path(config.database_path),
        service=service,
    )
    uvicorn_config = uvicorn.Config(
        create_asgi_app(state),
        host=_bind_host(config),
        port=config.port,
        log_level=config.log_level.lower(),
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_HTTP_TIMEOUT_SECONDS,
        lifespan="on",
    )
    server = _ServingServer(uvicorn_config, service)
    state.server = server
    return server


def run_collect(config: Config, *, service: Service | None = None) -> int:
    """Run the collector loop alone (Delivery 1 behaviour, now off the main thread)."""
    service = service or Service(config)
    service.start()
    service.wait_started()
    if service.startup_error is not None:
        log.error("startup failed; refusing to start: %s", _code_of(service.startup_error))
        return service.join(SHUTDOWN_BUDGET_SECONDS) or EXIT_STORAGE
    _install_plain_signal_handlers(service)
    try:
        service.wait_finished()
    finally:
        service.request_stop()
    return service.join(SHUTDOWN_BUDGET_SECONDS)


def run_serve(config: Config, *, service: Service | None = None) -> int:
    """Serve the API and collect in one process (spec D6)."""
    service = service or Service(config)
    server = build_server(config, service)
    service.start()
    service.wait_started()
    if service.startup_error is not None:
        log.error("startup failed; refusing to serve: %s", _code_of(service.startup_error))
        service.request_stop()
        return service.join(SHUTDOWN_BUDGET_SECONDS) or EXIT_STORAGE
    server.run()
    service.request_stop()
    return service.join(SHUTDOWN_BUDGET_SECONDS)


def run_restore(config: Config, input_path: object) -> int:
    """Restore a verified snapshot into the configured volume (offline, spec D8)."""
    from .restore import RestoreError, restore_volume  # local: avoids an import cycle

    try:
        info = restore_volume(
            config.database_path, input_path, source_id=config.source_id
        )
    except RestoreError as exc:
        log.error("restore refused: %s", exc.code)
        return EXIT_STORAGE
    print(info.describe())
    return EXIT_OK


def _code_of(error: BaseException | None) -> str:
    return getattr(error, "code", type(error).__name__)


def _install_plain_signal_handlers(service: Service) -> None:
    if threading.current_thread() is not threading.main_thread():
        return  # pragma: no cover - defensive

    def handler(signum, frame):  # noqa: ANN001 - signal signature
        service.request_stop()

    for sig in _HANDLED_SIGNALS:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)
