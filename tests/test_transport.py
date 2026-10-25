from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jarvis_dashboard.domain import COMPUTED_INPUT
from jarvis_dashboard.metrics import UpstreamError, UpstreamFetcher, parse_scrape
from fixture_server import FixtureServer, install_fixture

LABELS = {"model_name": "fixture-model", "engine": "0"}


def fetcher(server: FixtureServer, timeout: float = 3.0) -> UpstreamFetcher:
    return UpstreamFetcher(server.base_url, timeout)


def test_metrics_and_stats_roundtrip(fixture_server):
    install_fixture(fixture_server.state, LABELS, 1000, 100)
    text = fetcher(fixture_server).fetch_metrics_text()
    parsed = parse_scrape(text, datetime.now(UTC))
    assert parsed.for_labels(LABELS).values[COMPUTED_INPUT] == 1000
    stats = fetcher(fixture_server).fetch_stats()
    assert stats.model == "fixture-model"
    assert stats.uptime_s == 100.0
    assert stats.request_start <= stats.response_end


def test_http_500_maps_to_status_code(fixture_server):
    fixture_server.state.metrics_status = 500
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server).fetch("/metrics")
    assert e.value.code == "http_status_500"


def test_timeout_deadline_exceeded(fixture_server):
    fixture_server.state.delay_s = 0.5
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server, timeout=0.15).fetch("/metrics")
    assert e.value.code == "deadline_exceeded"


def test_slow_drip_exceeds_total_deadline(fixture_server):
    # 8 x 64KiB chunks with 60ms each = ~0.48s > 0.2s total budget
    fixture_server.state.chunk_delay_s = 0.06
    fixture_server.state.chunk_bytes = 65536
    fixture_server.state.metrics_body = "x" * (8 * 65536)
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server, timeout=0.2).fetch("/metrics")
    assert e.value.code == "deadline_exceeded"


def test_oversize_body_rejected(fixture_server):
    fixture_server.state.oversize = True
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server).fetch("/metrics")
    assert e.value.code == "response_too_large"


def test_redirect_rejected(fixture_server):
    fixture_server.state.redirect = True
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server).fetch("/metrics")
    assert e.value.code == "redirect_rejected"


def test_connection_failed_for_closed_port():
    with FixtureServer() as s:
        url = s.base_url
    with pytest.raises(UpstreamError) as e:
        UpstreamFetcher(url, 1.0).fetch("/metrics")
    assert e.value.code in ("connection_failed", "connect_timeout")


def test_stats_invalid_shape_rejected(fixture_server):
    fixture_server.state.stats_body = '{"uptime_s": 5}'  # missing model
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server).fetch_stats()
    assert e.value.code == "stats_missing_model"
    fixture_server.state.stats_body = '{"model": "m", "uptime_s": "abc"}'
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server).fetch_stats()
    assert e.value.code == "stats_invalid_uptime"
    fixture_server.state.stats_body = "not json"
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server).fetch_stats()
    assert e.value.code == "stats_invalid_json"


def test_stats_500_maps(fixture_server):
    fixture_server.state.stats_fail = True
    with pytest.raises(UpstreamError) as e:
        fetcher(fixture_server).fetch_stats()
    assert e.value.code == "http_status_500"


@pytest.fixture
def fixture_server():
    with FixtureServer() as s:
        yield s
