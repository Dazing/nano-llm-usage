"""Validated environment configuration (specification D2).

All values are read from the environment once at startup and validated
fail-fast: an invalid configuration must produce an actionable error before
any collection, storage access, or network request happens.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "http://192.168.1.228:8000"
DEFAULT_SOURCE_ID = "jarvis"
DEFAULT_POLL_INTERVAL_SECONDS = 15
DEFAULT_HTTP_TIMEOUT_SECONDS = 3.0
DEFAULT_DATABASE_PATH = "/data/dashboard.sqlite3"
DEFAULT_RAW_RETENTION_DAYS = 30
DEFAULT_PORT = 8080
DEFAULT_LOG_LEVEL = "INFO"

_ALLOWED_SCHEMES = ("http", "https")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ConfigError(ValueError):
    """Actionable startup configuration failure."""


@dataclass(frozen=True)
class Config:
    radiance_base_url: str
    source_id: str
    poll_interval_seconds: int
    http_timeout_seconds: float
    database_path: str
    raw_retention_days: int
    port: int
    log_level: str

    @property
    def metrics_url(self) -> str:
        return self.radiance_base_url + "/metrics"

    @property
    def stats_url(self) -> str:
        return self.radiance_base_url + "/stats"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        return from_env(environ)


def _parse_base_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ConfigError("RADIANCE_BASE_URL must not be empty")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ConfigError(f"RADIANCE_BASE_URL is not a valid URL: {value!r}") from exc
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise ConfigError(
            f"RADIANCE_BASE_URL scheme must be http or https, got {parts.scheme!r}: {value!r}"
        )
    if not parts.hostname:
        raise ConfigError(f"RADIANCE_BASE_URL must include a host: {value!r}")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("RADIANCE_BASE_URL must not contain credentials")
    if parts.fragment:
        raise ConfigError("RADIANCE_BASE_URL must not contain a fragment")
    if parts.query:
        raise ConfigError(
            "RADIANCE_BASE_URL must be an origin only (no query); fixed paths /metrics and /stats are appended"
        )
    path = parts.path.rstrip("/")
    if path:
        raise ConfigError(
            f"RADIANCE_BASE_URL must be an origin only (no path), got path {parts.path!r}"
        )
    # Rebuild the canonical origin so appended paths cannot escape it.
    try:
        port = parts.port
    except ValueError as exc:
        raise ConfigError(f"RADIANCE_BASE_URL has an invalid port: {value!r}") from exc
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    origin = f"{parts.scheme.lower()}://{host}"
    if port is not None:
        origin += f":{port}"
    return origin


def _parse_int(name: str, raw: str, low: int, high: int) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _parse_timeout(raw: str, poll_interval_seconds: int) -> float:
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"HTTP_TIMEOUT_SECONDS must be a number, got {raw!r}") from exc
    if not value > 0:
        raise ConfigError(f"HTTP_TIMEOUT_SECONDS must be positive, got {value}")
    if not math.isfinite(value):
        raise ConfigError("HTTP_TIMEOUT_SECONDS must be finite")
    if value >= poll_interval_seconds:
        raise ConfigError(
            f"HTTP_TIMEOUT_SECONDS ({value}) must be less than POLL_INTERVAL_SECONDS ({poll_interval_seconds})"
        )
    return value


def from_env(environ: dict[str, str] | None = None) -> Config:
    """Build a validated Config from the environment (defaults per specification D2)."""
    env = os.environ if environ is None else environ
    base_url = _parse_base_url(env.get("RADIANCE_BASE_URL", DEFAULT_BASE_URL))
    source_id = env.get("SOURCE_ID", DEFAULT_SOURCE_ID).strip()
    if not _SOURCE_ID_RE.fullmatch(source_id):
        raise ConfigError(
            "SOURCE_ID must be 1-64 characters from [A-Za-z0-9._-], starting alphanumeric; "
            "it is a durable identity and must never be derived from an IP address"
        )
    poll = _parse_int("POLL_INTERVAL_SECONDS", env.get("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS)), 5, 300)
    timeout = _parse_timeout(env.get("HTTP_TIMEOUT_SECONDS", str(DEFAULT_HTTP_TIMEOUT_SECONDS)), poll)
    database_path = env.get("DATABASE_PATH", DEFAULT_DATABASE_PATH).strip()
    if not database_path:
        raise ConfigError("DATABASE_PATH must not be empty")
    retention = _parse_int("RAW_RETENTION_DAYS", env.get("RAW_RETENTION_DAYS", str(DEFAULT_RAW_RETENTION_DAYS)), 1, 365)
    port = _parse_int("PORT", env.get("PORT", str(DEFAULT_PORT)), 1, 65535)
    log_level = env.get("LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}, got {log_level!r}")
    return Config(
        radiance_base_url=base_url,
        source_id=source_id,
        poll_interval_seconds=poll,
        http_timeout_seconds=timeout,
        database_path=database_path,
        raw_retention_days=retention,
        port=port,
        log_level=log_level,
    )
