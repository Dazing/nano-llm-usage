"""Validated environment configuration (specification D2).

All values are read from the environment once at startup and validated
fail-fast: an invalid configuration must produce an actionable error before
any collection, storage access, or network request happens.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "http://192.168.1.228:8000"
DEFAULT_SOURCE_ID = "jarvis"
DEFAULT_POLL_INTERVAL_SECONDS = 15
DEFAULT_HTTP_TIMEOUT_SECONDS = 3.0
DEFAULT_DATABASE_PATH = "/data/dashboard.sqlite3"
# Six months of detailed samples/events. Only raw detail expires: hourly
# totals, epochs, baselines and collector state never do (specification D5).
DEFAULT_RAW_RETENTION_DAYS = 180
DEFAULT_PORT = 8080
DEFAULT_BIND_ADDRESS = "127.0.0.1"
DEFAULT_LOG_LEVEL = "INFO"

# API-equivalent cost estimate (cost plan D3): one server-wide price set, all
# three variables or none, validated at startup and never stored in the database.
COST_RATE_ENV_NAMES = (
    "COST_RATE_INPUT_PER_MTOK",
    "COST_RATE_CACHED_INPUT_PER_MTOK",
    "COST_RATE_OUTPUT_PER_MTOK",
)
MAX_COST_RATE = Decimal(10000)
MAX_COST_RATE_DECIMAL_PLACES = 6
_COST_CURRENCY = "USD"

_ALLOWED_SCHEMES = ("http", "https")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DECIMAL_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


class ConfigError(ValueError):
    """Actionable startup configuration failure."""


@dataclass(frozen=True)
class CostRates:
    """Per-million-token API list prices (cost plan D3).

    ``Decimal`` end to end: a float would silently round money, and the estimate
    is only trustworthy if the arithmetic matches the operator's typed price.
    ``str(Decimal)`` reproduces the typed lexeme (``0.30`` stays ``0.30``), so
    the wire form never shows a binary-rounded value.
    """

    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    output_per_mtok: Decimal
    currency: str = _COST_CURRENCY

    def as_wire(self) -> dict[str, str]:
        return {
            "currency": self.currency,
            "input_per_mtok": str(self.input_per_mtok),
            "cached_input_per_mtok": str(self.cached_input_per_mtok),
            "output_per_mtok": str(self.output_per_mtok),
        }


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
    bind_address: str = DEFAULT_BIND_ADDRESS
    static_dir: str | None = None
    cost_rates: CostRates | None = None

    @property
    def metrics_url(self) -> str:
        return self.radiance_base_url + "/metrics"

    @property
    def stats_url(self) -> str:
        return self.radiance_base_url + "/stats"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        return from_env(environ)


def _parse_bind_address(raw: str) -> str:
    """An explicit IP literal: never a hostname, never an implicit wildcard."""
    value = raw.strip()
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ConfigError(
            f"JARVIS_BIND_ADDRESS must be an IP literal (e.g. {DEFAULT_BIND_ADDRESS}), got {raw!r}"
        ) from exc
    return value


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


def _parse_cost_rate(name: str, raw: str) -> Decimal:
    """One price: finite decimal, 0..10000, at most 6 decimal places, never float."""
    if not _DECIMAL_RE.fullmatch(raw):
        raise ConfigError(
            f"{name} must be a decimal price per million tokens (e.g. 0.30), got {raw!r}"
        )
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - the regex above rejects these
        raise ConfigError(f"{name} must be a decimal price, got {raw!r}") from exc
    if not value.is_finite():
        raise ConfigError(f"{name} must be a finite decimal price, got {raw!r}")
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {raw!r}")
    if value > MAX_COST_RATE:
        raise ConfigError(f"{name} must be at most {MAX_COST_RATE}, got {raw!r}")
    places = max(0, -value.as_tuple().exponent)
    if places > MAX_COST_RATE_DECIMAL_PLACES:
        raise ConfigError(
            f"{name} must have at most {MAX_COST_RATE_DECIMAL_PLACES} decimal places, got {raw!r}"
        )
    return value


def _parse_cost_rates(env: dict[str, str]) -> CostRates | None:
    """All three prices, or none: a partial set is a startup failure (cost plan D3).

    A half-configured price set would silently mis-price every range, so missing
    variables are named explicitly and the operator fixes the set, not the math.
    """
    raw: dict[str, str | None] = {}
    for name in COST_RATE_ENV_NAMES:
        value = (env.get(name) or "").strip()
        raw[name] = value or None
    present = [name for name in COST_RATE_ENV_NAMES if raw[name] is not None]
    if not present:
        return None
    if len(present) != len(COST_RATE_ENV_NAMES):
        missing = [name for name in COST_RATE_ENV_NAMES if raw[name] is None]
        raise ConfigError(
            "cost rates must be set together; missing "
            f"{', '.join(missing)} (set all of {', '.join(COST_RATE_ENV_NAMES)}, "
            "or none of them to disable the API-equivalent estimate)"
        )
    return CostRates(
        input_per_mtok=_parse_cost_rate(COST_RATE_ENV_NAMES[0], raw[COST_RATE_ENV_NAMES[0]] or ""),
        cached_input_per_mtok=_parse_cost_rate(
            COST_RATE_ENV_NAMES[1], raw[COST_RATE_ENV_NAMES[1]] or ""
        ),
        output_per_mtok=_parse_cost_rate(COST_RATE_ENV_NAMES[2], raw[COST_RATE_ENV_NAMES[2]] or ""),
    )


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
    bind_address = _parse_bind_address(env.get("JARVIS_BIND_ADDRESS", DEFAULT_BIND_ADDRESS))
    raw_static = env.get("STATIC_DIR")
    static_dir = raw_static.strip() if raw_static is not None and raw_static.strip() else None
    if raw_static is not None and raw_static != "" and static_dir is None:
        raise ConfigError("STATIC_DIR must not be empty")
    return Config(
        radiance_base_url=base_url,
        source_id=source_id,
        poll_interval_seconds=poll,
        http_timeout_seconds=timeout,
        database_path=database_path,
        raw_retention_days=retention,
        port=port,
        log_level=log_level,
        bind_address=bind_address,
        static_dir=static_dir,
        cost_rates=_parse_cost_rates(env),
    )
