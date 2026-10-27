from __future__ import annotations

from decimal import Decimal

import pytest

from jarvis_dashboard.config import Config, ConfigError, CostRates

BASE = {
    "RADIANCE_BASE_URL": "http://192.168.1.228:8000",
    "SOURCE_ID": "jarvis",
    "DATABASE_PATH": "/tmp/x.db",
}
RATES = {
    "COST_RATE_INPUT_PER_MTOK": "3",
    "COST_RATE_CACHED_INPUT_PER_MTOK": "0.30",
    "COST_RATE_OUTPUT_PER_MTOK": "15",
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
    assert c.raw_retention_days == 180
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


def test_static_dir_defaults_to_the_packaged_build():
    # Absent and empty both mean "use the directory beside the package" (D1).
    assert Config.from_env(env()).static_dir is None
    assert Config.from_env(env(STATIC_DIR="")).static_dir is None


def test_static_dir_override_is_stripped():
    assert Config.from_env(env(STATIC_DIR=" /srv/static ")).static_dir == "/srv/static"


def test_blank_static_dir_rejected_with_actionable_message():
    # Whitespace-only would silently serve the packaged build instead of the
    # operator's mount, so it is a configuration error, not a default.
    with pytest.raises(ConfigError, match="STATIC_DIR"):
        Config.from_env(env(STATIC_DIR="   "))


def test_cost_rates_unset_by_default():
    # AC4: no rates means no estimate, and it is not an error.
    assert Config.from_env(env()).cost_rates is None


def test_blank_cost_rates_mean_unset():
    # Compose passes `:-` empty defaults through, so empty is the unset case.
    c = Config.from_env(
        env(COST_RATE_INPUT_PER_MTOK="", COST_RATE_CACHED_INPUT_PER_MTOK="  ", COST_RATE_OUTPUT_PER_MTOK="")
    )
    assert c.cost_rates is None


def test_all_three_cost_rates_enable_the_estimate():
    rates = Config.from_env(env(**RATES)).cost_rates
    assert rates == CostRates(
        input_per_mtok=Decimal("3"),
        cached_input_per_mtok=Decimal("0.30"),
        output_per_mtok=Decimal("15"),
    )
    # Decimal text survives verbatim: no 0.3 -> 0.30000000000000004 float drift.
    assert rates.as_wire() == {
        "currency": "USD",
        "input_per_mtok": "3",
        "cached_input_per_mtok": "0.30",
        "output_per_mtok": "15",
    }


def test_zero_prices_are_valid():
    rates = Config.from_env(
        env(
            COST_RATE_INPUT_PER_MTOK="0",
            COST_RATE_CACHED_INPUT_PER_MTOK="0",
            COST_RATE_OUTPUT_PER_MTOK="0",
        )
    ).cost_rates
    assert rates is not None and rates.output_per_mtok == Decimal("0")


@pytest.mark.parametrize("missing", [
    ["COST_RATE_INPUT_PER_MTOK"],
    ["COST_RATE_CACHED_INPUT_PER_MTOK"],
    ["COST_RATE_OUTPUT_PER_MTOK"],
    ["COST_RATE_INPUT_PER_MTOK", "COST_RATE_OUTPUT_PER_MTOK"],
])
def test_partial_cost_rates_name_every_missing_variable(missing):
    overrides = dict(RATES)
    for name in missing:
        overrides[name] = None
    with pytest.raises(ConfigError) as exc:
        Config.from_env(env(**overrides))
    for name in missing:
        assert name in str(exc.value)


@pytest.mark.parametrize("bad", ["-1", "abc", "NaN", "Infinity", "10001", "0.1234567", "1e999", "1.2.3", "  "])
def test_invalid_cost_rate_values_rejected(bad):
    for name in RATES:
        with pytest.raises(ConfigError, match=name):
            Config.from_env(env(**{**RATES, name: bad}))


def test_cost_rate_bounds_are_inclusive():
    rates = Config.from_env(
        env(
            COST_RATE_INPUT_PER_MTOK="10000",
            COST_RATE_CACHED_INPUT_PER_MTOK="0.000001",
            COST_RATE_OUTPUT_PER_MTOK="0.5",
        )
    ).cost_rates
    assert rates is not None
    assert rates.input_per_mtok == Decimal("10000")
    assert rates.cached_input_per_mtok == Decimal("0.000001")
