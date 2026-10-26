"""Command-line entry point: ``collect``, ``serve``, ``backup``, ``restore``.

All long-running commands share one lifecycle: the writer thread owns the volume
lock and the only writable connection, so ``collect`` and ``serve`` refuse a
second instance instead of waiting for the lock.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import service as service_mod
from .backup import BackupError, create_snapshot, describe
from .config import Config, ConfigError

EXIT_RUNTIME = service_mod.EXIT_RUNTIME
EXIT_STORAGE = service_mod.EXIT_STORAGE


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    # Payload-free logging: messages above never include response bodies.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis-dashboard")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="run the durable token collector")
    sub.add_parser("serve", help="serve the JSON API and collect tokens")
    backup = sub.add_parser("backup", help="write a verified online snapshot")
    backup.add_argument("--output", required=True, type=Path, help="new snapshot file")
    restore = sub.add_parser("restore", help="restore a snapshot into a stopped volume")
    restore.add_argument("--input", required=True, type=Path, help="snapshot file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(config.log_level)
    log = logging.getLogger("jarvis_dashboard.cli")

    if args.command == "backup":
        return _run_backup(config, args.output, log)
    if args.command == "restore":
        return service_mod.run_restore(config, args.input)
    if args.command == "serve":
        log.info(
            "dashboard serving",
            extra={"source_id": config.source_id, "port": config.port},
        )
        return service_mod.run_serve(config)
    return service_mod.run_collect(config)


def _run_backup(config: Config, output: Path, log: logging.Logger) -> int:
    try:
        info = create_snapshot(Path(config.database_path), output)
    except BackupError as exc:
        log.error("backup refused: %s", exc.code)
        return EXIT_STORAGE
    print(describe(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
