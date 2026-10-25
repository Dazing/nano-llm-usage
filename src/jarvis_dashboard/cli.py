"""Command-line entry point: ``jarvis-dashboard collect``.

Delivery 1 ships the collector only; no API/dashboard surfaces yet.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from .collector import Collector
from .config import Config, ConfigError
from .storage import StorageError, acquire_volume_lock, open_storage


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    # Payload-free logging: messages above never include response bodies.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-dashboard")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="run the durable token collector")
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(config.log_level)
    log = logging.getLogger("jarvis_dashboard.cli")

    db_path = Path(config.database_path)
    try:
        lock = acquire_volume_lock(db_path)
    except StorageError as exc:
        log.error("refusing to start: %s", exc)
        return 3
    try:
        conn = open_storage(db_path)
    except StorageError as exc:
        log.error("refusing to open database: %s", exc)
        lock.release()
        return 3

    collector = Collector(config, conn)
    stop_requested = False

    def _handle(signum, frame):  # noqa: ANN001
        nonlocal stop_requested
        if not stop_requested:
            stop_requested = True
            log.info("signal %s received, shutting down", signum)
            collector.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    log.info(
        "collector starting",
        extra={"source_id": config.source_id, "endpoint": config.radiance_base_url},
    )
    try:
        collector.run_forever()
    finally:
        conn.close()
        lock.release()
        log.info("collector stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
