from __future__ import annotations

import pytest

from jarvis_dashboard.config import Config, ConfigError

BASE = {
    "RADIANCE_BASE_URL": "http://192.168.1.228:8000",
    "SOURCE_ID": "jarvis",
    "DATABASE_PATH": "/tmp/x.db",
}


def env(**overrides) -> dict[str, str]:
    e = dict(BASE)
    for k, v in overrides.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


def test_defaults_match_spec():
    c = Config.from_env(env())
    assert c.radiance_base_url == "http://192.168.1.228:8000"
    assert c.source_id == "jarvis"
    assert c.poll_interval_seconds == 15
    assert c.http_timeout_seconds == 3.0
    assert c.raw_retention_days == 30
    assert c.port == 8080
    assert c.log_level == "INFO"
    assert c.metrics_url == "http://192.168.1.228:8000/metrics"
    assert c.stats_url == "http://192.168.1.228:8000/stats"


def test_missing_base_url_uses_documented_default():
    # Specification D2 gives an operator default; absence is not an error.
    assert Config.from_env(env(RADIANCE_BASE_URL=None)).radiance_base_url == "http://192.168.1.228:8000"


def test_empty_base_url_rejected_with_actionable_message():
    with pytest.raises(ConfigError, match="RADIANCE_BASE_URL"):
        Config.from_env(env(RADIANCE_BASE_URL=""))


@pytest.mark.parametrize(
    "raw",
    [
        "192.168.1.228:8000",           # missing scheme
        "ftp://host",                    # non-http scheme
        "http://",                       # missing host
        "http://user:pw@host",           # credentials forbidden
        "http://host:8000/metrics",      # path forbidden (would double-append)
        "http://host?q=1",               # query forbidden
        "http://host#frag",              # fragment forbidden
        "",
    ],
)
def test_bad_base_urls_rejected(raw):
    with pytest.raises(ConfigError):
        Config.from_env(env(RADIANCE_BASE_URL=raw))


def test_trailing_slash_normalised_to_origin():
    assert Config.from_env(env(RADIANCE_BASE_URL="http://host:8000/")).radiance_base_url == "http://host:8000"


def test_poll_interval_bounds_inclusive():
    assert Config.from_env(env(POLL_INTERVAL_SECONDS="5")).poll_interval_seconds == 5
    assert Config.from_env(env(POLL_INTERVAL_SECONDS="300")).poll_interval_seconds == 300
    for bad in ("4", "301", "0", "-1", "abc"):
        with pytest.raises(ConfigError):
            Config.from_env(env(POLL_INTERVAL_SECONDS=bad))


def test_timeout_must_be_positive_and_less_than_poll():
    with pytest.raises(ConfigError):
        Config.from_env(env(HTTP_TIMEOUT_SECONDS="0"))
    with pytest.raises(ConfigError):
        Config.from_env(env(HTTP_TIMEOUT_SECONDS="15", POLL_INTERVAL_SECONDS="15"))
    assert Config.from_env(env(HTTP_TIMEOUT_SECONDS="14.9")).http_timeout_seconds == 14.9


def test_timeout_validated_against_explicit_poll_interval():
    # poll=10, timeout=12 -> invalid even though default timeout 3 would pass
    with pytest.raises(ConfigError):
        Config.from_env(env(POLL_INTERVAL_SECONDS="10", HTTP_TIMEOUT_SECONDS="12"))


@pytest.mark.parametrize("bad", ["", "jar vis", "_lead", "a" * 65, "a/b"])
def test_source_id_rules(bad):
    with pytest.raises(ConfigError):
        Config.from_env(env(SOURCE_ID=bad))


def test_source_id_accepts_legal_forms():
    assert Config.from_env(env(SOURCE_ID="a.b-c_1")).source_id == "a.b-c_1"


def test_retention_and_port_bounds():
    for bad in ("0", "366"):
        with pytest.raises(ConfigError):
            Config.from_env(env(RAW_RETENTION_DAYS=bad))
    for bad in ("0", "65536"):
        with pytest.raises(ConfigError):
            Config.from_env(env(PORT=bad))


def test_unknown_log_level_rejected():
    with pytest.raises(ConfigError):
        Config.from_env(env(LOG_LEVEL="TRACE"))
