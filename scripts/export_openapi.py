#!/usr/bin/env python3
"""OpenAPI snapshot writer and freshness gate for the generated frontend types.

The dashboard's TypeScript types are generated from ``frontend/src/api/openapi.json``,
so that file must be the document the running app actually serves. It is produced
in-process from the same call ``create_app`` makes — no database, no network — and
``--check`` fails whenever the snapshot has drifted from the live schema, so a
backend contract change breaks the frontend build instead of the runtime.

Usage:
    .venv/bin/python scripts/export_openapi.py            # write the snapshot
    .venv/bin/python scripts/export_openapi.py --check    # exit 1 when stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jarvis_dashboard.app import AppState, create_app  # noqa: E402
from jarvis_dashboard.config import Config  # noqa: E402
from jarvis_dashboard.lifecycle import RuntimeHealth  # noqa: E402

SNAPSHOT = ROOT / "frontend" / "src" / "api" / "openapi.json"
# Never opened: the schema is produced from the models alone, so no volume exists.
UNUSED_DB_PATH = "/tmp/jarvis-openapi-none.sqlite3"


def live_document() -> dict[str, Any]:
    """The frozen OpenAPI document exactly as ``create_app`` publishes it."""
    config = Config(
        "http://127.0.0.1:8000",
        "jarvis",
        15,
        3.0,
        UNUSED_DB_PATH,
        30,
        8080,
        "INFO",
    )
    state = AppState(
        config=config,
        health=RuntimeHealth(poll_interval_seconds=15),
        db_path=Path(UNUSED_DB_PATH),
    )
    return create_app(state).openapi_schema


def live_bytes() -> bytes:
    return (json.dumps(live_document(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def first_difference(expected: Any, actual: Any, path: str = "$") -> str:
    """Human-readable JSON key path of the first difference (for the failure line)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                return f"{path}.{key} added by the live schema"
            if key not in actual:
                return f"{path}.{key} missing from the live schema"
            found = first_difference(expected[key], actual[key], f"{path}.{key}")
            if found:
                return found
        return ""
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return f"{path} length {len(actual)} != {len(expected)}"
        for index, (want, have) in enumerate(zip(expected, actual)):
            found = first_difference(want, have, f"{path}[{index}]")
            if found:
                return found
        return ""
    if expected != actual:
        return f"{path} changed"
    return ""


def check(snapshot: Path, payload: bytes) -> int:
    if not snapshot.is_file():
        print(f"openapi snapshot missing: run scripts/export_openapi.py ({snapshot})", file=sys.stderr)
        return 1
    stored = snapshot.read_bytes()
    if stored == payload:
        return 0
    try:
        difference = first_difference(json.loads(payload.decode()), json.loads(stored.decode()))
    except ValueError:
        difference = "snapshot is not valid JSON"
    print(f"openapi snapshot is stale: {difference or 'bytes differ'}", file=sys.stderr)
    print("run: .venv/bin/python scripts/export_openapi.py", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    check_only = "--check" in argv[1:]
    if len(argv) > 1 and not check_only:
        print(f"usage: {Path(argv[0]).name} [--check]", file=sys.stderr)
        return 2
    try:
        payload = live_bytes()
    except Exception as exc:  # one actionable line, never a traceback
        print(f"openapi export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if check_only:
        return check(SNAPSHOT, payload)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_bytes(payload)
    document = json.loads(payload.decode())
    print(f"wrote {SNAPSHOT} ({len(document['paths'])} paths, {len(document['components']['schemas'])} schemas)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
