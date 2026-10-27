"""Bounded ``/api/v1`` routes (Delivery 2 Step 5, spec D2/D3/D5/D6).

Distinguishing scenarios: every route answers from the durable database with the
frozen envelope; token values are exact decimal strings; limits, enums, cursors,
and ranges produce the documented 400 codes; an unknown series is 404; four
concurrent snapshot reads are served and the fifth is refused immediately with
``read_capacity_exceeded`` (no queueing); storage failure is 503; unexpected
failures stay payload-free; ``Cache-Control: no-store`` covers errors too.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from asgi_client import call, request
from test_storage_helpers import SOURCE, T0, apply, seed_chain
from urllib.parse import quote

from jarvis_dashboard import queries, storage
from jarvis_dashboard.api import ReadAdmission
from jarvis_dashboard.app import AppState, create_app, create_asgi_app
from jarvis_dashboard.accounting import transition_series
from jarvis_dashboard.config import Config
from jarvis_dashboard.domain import (
    ScrapeCounters,
    StatsEvidence,
    canonical_labels_json,
    to_iso,
    utc_now,
)
from jarvis_dashboard.lifecycle import RuntimeHealth
from jarvis_dashboard.queries import encode_series_id

SERIES_ID = encode_series_id(SOURCE, '{"engine":"0","model_name":"m"}')
A2_STEPS = [(1000, 100), (1600, 140), (1900, 160)]


def make_config(db_path: Path) -> Config:
    return Config(
        "http://127.0.0.1:8000", SOURCE, 15, 3.0, str(db_path), 30, 8080, "INFO"
    )


def make_health(ready: bool = True) -> RuntimeHealth:
    """Health whose recency is measured against the wall clock the route uses."""
    health = RuntimeHealth(poll_interval_seconds=15)
    health.mark_started(utc_now())
    if ready:
        health.mark_writer_ready()
        health.mark_primary_success(utc_now())
    return health


def seed_reset(conn, model: str, computed: int, generated: int, at) -> None:
    """Continue an existing chain with lower counters: a real counter reset."""
    labels = {"model_name": model, "engine": "0"}
    identity = canonical_labels_json(labels)
    state = storage.load_series(conn, SOURCE)[identity].state
    scrape = ScrapeCounters(
        labels,
        {
            "computed_input": computed,
            "generated_output": generated,
            "request_input": 100,
            "request_output": 10,
        },
        {},
    )
    evidence = StatsEvidence(model, 1000 + (at - T0).total_seconds(), at, at)
    plan = transition_series(state, scrape, at, evidence, 15)
    apply(conn, f"reset-{model}-{to_iso(at)}", [plan], at)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "d.sqlite3"
    conn = storage.open_storage(path)
    seed_chain(conn, "m", A2_STEPS, T0)
    conn.close()
    return path


def app_for(db: Path, health: RuntimeHealth | None = None, **config_kw):
    config = make_config(db)
    for key, value in config_kw.items():
        object.__setattr__(config, key, value)
    return create_app(AppState(config, health or make_health(), db))


def get(app, path: str, query: str = "", **kw):
    return request(app, "GET", path, query=query, **kw)


def envelope_ok(response) -> dict:
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["schema_version"] == 1
    assert body["generated_at"].endswith("Z")
    return body


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def test_status_reports_durable_activity_and_never_the_endpoint(db):
    response = get(app_for(db), "/api/v1/status")
    body = envelope_ok(response)
    assert response.status_code == 200
    assert body["source_id"] == SOURCE
    assert body["poll_interval_seconds"] == 15
    assert body["raw_retention_days"] == 30
    assert body["series_count"] == 1
    assert body["first_observation_at"] == to_iso(T0)
    assert body["latest_observed_at"] == to_iso(T0 + timedelta(seconds=30))
    assert body["collector_status"] == "healthy"
    assert "8000" not in response.text
    assert "http" not in response.text
    assert "fingerprint" not in response.text


def test_status_of_an_unreadable_volume_is_a_503(tmp_path):
    app = app_for(tmp_path / "missing.sqlite3")
    response = get(app, "/api/v1/status")
    assert response.status_code == 503
    body = envelope_ok(response)
    assert body["error"]["code"] == "storage_unavailable"


def test_unexpected_read_failure_stays_payload_free(db, monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("secret upstream response body")

    monkeypatch.setattr(queries, "read_status", boom)
    app = create_asgi_app(AppState(make_config(db), make_health(), db))
    response = get(app, "/api/v1/status", swallow_server_exception=True)
    assert response.status_code == 500
    body = envelope_ok(response)
    assert body["error"]["code"] == "internal_error"
    assert "secret" not in response.text


# --------------------------------------------------------------------------- #
# series
# --------------------------------------------------------------------------- #
def test_series_page_is_bounded_and_paginates_by_keyset(db):
    app = app_for(db)
    for index in range(5):
        conn = storage.open_storage(db)
        seed_chain(conn, f"model-{index}", A2_STEPS, T0)
        conn.close()
    first = envelope_ok(get(app, "/api/v1/series", "limit=2"))
    assert first["page_size"] == 2
    assert [item["model_name"] for item in first["items"]] == ["m", "model-0"]
    assert first["items"][0]["series_id"] == SERIES_ID
    assert first["items"][0]["state"] == "stale"
    assert first["next_cursor"]
    second = envelope_ok(
        get(app, "/api/v1/series", f"limit=2&cursor={first['next_cursor']}")
    )
    assert [item["model_name"] for item in second["items"]] == ["model-1", "model-2"]
    third = envelope_ok(
        get(app, "/api/v1/series", f"limit=2&cursor={second['next_cursor']}")
    )
    assert [item["model_name"] for item in third["items"]] == ["model-3", "model-4"]
    assert third["next_cursor"] is None


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=abc", "cursor=" + "x" * 600])
def test_series_limits_use_the_common_400_model(db, query):
    response = get(app_for(db), "/api/v1/series", query)
    assert response.status_code == 400
    body = envelope_ok(response)
    assert body["error"] == {
        "code": "invalid_request",
        "message": "request parameters are invalid",
    }


def test_series_cursor_cannot_be_reused_on_another_route(db):
    app = app_for(db)
    for index in range(3):
        conn = storage.open_storage(db)
        seed_chain(conn, f"model-{index}", A2_STEPS, T0)
        conn.close()
    cursor = envelope_ok(get(app, "/api/v1/series", "limit=2"))["next_cursor"]
    response = get(app, "/api/v1/events", f"series_id={SERIES_ID}&limit=2&cursor={cursor}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #
def test_summary_serves_the_exact_a2_totals_as_decimal_strings(db):
    response = get(app_for(db), "/api/v1/summary", f"series_id={SERIES_ID}")
    body = envelope_ok(response)
    assert response.status_code == 200
    lifetime = body["ranges"]["lifetime"]
    assert lifetime["counters"]["computed_input"]["tokens"] == "900"
    assert lifetime["counters"]["generated_output"]["tokens"] == "60"
    assert lifetime["processed_total"] == "960"
    assert body["ranges"]["today"]["observed"] is False
    assert body["ranges"]["month"]["observed"] is False
    assert isinstance(lifetime["counters"]["computed_input"]["observed_intervals"], int)
    assert body["rates"]["computed_input"]["tokens_per_second"] == pytest.approx(20.0)
    assert body["epoch"]["reason"] == "new_series"
    assert body["epoch"]["opening_computed_input"] == "1000"
    assert body["epochs"]["items"][0]["epoch_id"] == body["epoch"]["epoch_id"]
    assert body["epochs"]["next_cursor"] is None


def test_sums_past_two_to_the_53_stay_exact_decimal_strings(db):
    """D2-AC1: large exact integers must not pass through a float."""
    conn = storage.open_storage(db)
    identity = '{"engine":"0","model_name":"m"}'
    for hour, tokens in (("2026-01-15T11Z", 9007199254740991), ("2026-01-15T12Z", 2)):
        conn.execute(
            "INSERT INTO hourly_totals (source_id, identity, hour, counter_key, tokens,"
            " observed_intervals, quality_json, metadata_complete, first_observed_at,"
            " last_observed_at) VALUES (?, ?, ?, 'computed_input', ?, 1, '[]', 1, ?, ?)",
            (
                SOURCE,
                identity,
                hour,
                tokens,
                f"{hour[:-1]}:05.000000Z",
                f"{hour[:-1]}:05.000000Z",
            ),
        )
    conn.commit()
    conn.close()

    response = get(app_for(db), "/api/v1/summary", f"series_id={SERIES_ID}")
    body = envelope_ok(response)
    computed = 900 + 9007199254740991 + 2
    assert body["ranges"]["lifetime"]["counters"]["computed_input"]["tokens"] == str(computed)
    assert body["ranges"]["lifetime"]["processed_total"] == str(computed + 60)
    assert str(computed) in response.text


def test_each_response_uses_one_read_snapshot_and_closes_it(db, monkeypatch):
    """D2-AC3: one read transaction per response, connection never leaked."""
    opened: list[sqlite3.Connection] = []
    real = storage.read_snapshot

    @contextlib.contextmanager
    def spy(db_path):
        with real(db_path) as conn:
            opened.append(conn)
            yield conn

    monkeypatch.setattr(storage, "read_snapshot", spy)
    assert get(app_for(db), "/api/v1/summary", f"series_id={SERIES_ID}").status_code == 200
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_summary_rejects_unknown_and_malformed_series_ids(db):
    app = app_for(db)
    unknown = encode_series_id(SOURCE, '{"engine":"0","model_name":"absent"}')
    response = get(app, "/api/v1/summary", f"series_id={unknown}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "series_not_found"
    response = get(app, "/api/v1/summary", "series_id=v1.not-base64")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_series_id"
    response = get(app, "/api/v1/summary", "")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_summary_epoch_page_is_bounded(db):
    app = app_for(db)
    conn = storage.open_storage(db)
    for index in range(2):
        seed_reset(conn, "m", 50 - 10 * index, 5 - index, T0 + timedelta(hours=index + 1))
    conn.close()
    body = envelope_ok(get(app, "/api/v1/summary", f"series_id={SERIES_ID}&epoch_limit=2"))
    assert [item["reason"] for item in body["epochs"]["items"]] == [
        "counter_reset",
        "counter_reset",
    ]
    assert body["epochs"]["next_cursor"]
    page = envelope_ok(
        get(
            app,
            "/api/v1/summary",
            f"series_id={SERIES_ID}&epoch_limit=2&epoch_cursor={body['epochs']['next_cursor']}",
        )
    )
    assert [item["reason"] for item in page["epochs"]["items"]] == ["new_series"]
    assert page["epochs"]["next_cursor"] is None
    response = get(app, "/api/v1/summary", f"series_id={SERIES_ID}&epoch_limit=21")
    assert response.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #
def test_history_generates_every_bucket_and_excludes_the_end(db):
    app = app_for(db)
    query = (
        f"series_id={SERIES_ID}&bucket=hour"
        f"&from={to_iso(T0)}&to={to_iso(T0 + timedelta(hours=3))}"
    )
    body = envelope_ok(get(app, "/api/v1/history", query))
    assert body["bucket_count"] == 3
    assert [bucket["bucket"] for bucket in body["buckets"]] == [
        "2026-01-15T10Z",
        "2026-01-15T11Z",
        "2026-01-15T12Z",
    ]
    assert body["buckets"][0]["counters"]["computed_input"]["tokens"] == "900"
    # Empty buckets are generated with nulls, not dropped or clamped.
    assert body["buckets"][1]["counters"]["computed_input"]["tokens"] is None
    assert body["buckets"][1]["counters"]["computed_input"]["observed_intervals"] == 0
    assert body["buckets"][2]["counters"]["computed_input"]["tokens"] is None
    assert body["range"]["counters"]["computed_input"]["tokens"] == "900"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"from": "2026-01-15T10:00:00"}, "invalid_time"),  # naive
        ({"from": "2026-01-15T10:00:00+02:00"}, "invalid_time"),  # offset
        ({"from": "not-a-time"}, "invalid_time"),
        ({"bucket": "minute"}, "invalid_bucket"),
    ],
)
def test_history_rejects_bad_times_and_buckets(db, overrides, code):
    params = {
        "series_id": SERIES_ID,
        "from": to_iso(T0),
        "to": to_iso(T0 + timedelta(hours=1)),
        "bucket": "hour",
    }
    params.update(overrides)
    query = "&".join(f"{key}={quote(value, safe='')}" for key, value in params.items())
    response = get(app_for(db), "/api/v1/history", query)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_history_range_limits_are_exact(db):
    app = app_for(db)
    base = f"series_id={SERIES_ID}&bucket=hour&from={to_iso(T0)}"
    ok = get(app, "/api/v1/history", f"{base}&to={to_iso(T0 + timedelta(hours=744))}")
    assert ok.status_code == 200
    assert ok.json()["bucket_count"] == 744
    too_big = get(app, "/api/v1/history", f"{base}&to={to_iso(T0 + timedelta(hours=745))}")
    assert too_big.status_code == 400
    assert too_big.json()["error"]["code"] == "range_too_large"
    days = get(
        app,
        "/api/v1/history",
        f"series_id={SERIES_ID}&bucket=day&from={to_iso(T0.replace(hour=0, minute=0))}"
        f"&to={to_iso(T0.replace(hour=0, minute=0) + timedelta(days=366))}",
    )
    assert days.status_code == 200
    assert days.json()["bucket_count"] == 366
    unaligned = get(
        app,
        "/api/v1/history",
        f"series_id={SERIES_ID}&bucket=hour&from={to_iso(T0 + timedelta(minutes=15))}"
        f"&to={to_iso(T0 + timedelta(hours=2))}",
    )
    assert unaligned.json()["error"]["code"] == "unaligned_range"
    inverted = get(
        app,
        "/api/v1/history",
        f"series_id={SERIES_ID}&bucket=hour&from={to_iso(T0 + timedelta(hours=1))}"
        f"&to={to_iso(T0)}",
    )
    assert inverted.json()["error"]["code"] == "invalid_range"


def test_history_reports_gap_seconds_from_recorded_spans(db):
    app = app_for(db)
    query = (
        f"series_id={SERIES_ID}&bucket=hour&from={to_iso(T0)}"
        f"&to={to_iso(T0 + timedelta(hours=1))}"
    )
    body = envelope_ok(get(app, "/api/v1/history", query))
    # The series stopped being observed at 10:00:30; downtime starts after two
    # 15 s intervals and is counted up to the request instant.
    gaps = body["buckets"][0]["gap_seconds"]
    assert gaps["primary"] > 0
    assert body["buckets"][0]["partial"] is True
    assert body["buckets"][0]["counters"]["computed_input"]["gap_seconds"] == gaps["primary"]


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def test_events_page_descends_and_discloses_retention(db):
    conn = storage.open_storage(db)
    for index, kind in enumerate(["new_series", "gap_closed", "counter_reset"]):
        conn.execute(
            "INSERT INTO events (source_id, identity, kind, occurred_at, details_json)"
            " VALUES (?, ?, ?, ?, '{}')",
            (SOURCE, '{"engine":"0","model_name":"m"}', kind, to_iso(T0 + timedelta(seconds=index))),
        )
    conn.execute(
        "INSERT INTO events (source_id, identity, kind, occurred_at, details_json)"
        " VALUES (?, NULL, 'retention', ?, '{}')",
        (SOURCE, to_iso(T0 + timedelta(seconds=5))),
    )
    conn.commit()
    conn.close()

    app = app_for(db)
    body = envelope_ok(get(app, "/api/v1/events", f"series_id={SERIES_ID}&limit=2"))
    assert body["page_size"] == 2
    assert [item["kind"] for item in body["items"]] == ["retention", "counter_reset"]
    assert body["items"][0]["series_id"] is None  # source-wide events are included
    assert body["items"][1]["series_id"] == SERIES_ID
    assert body["retention"]["raw_retention_days"] == 30
    assert "30" in body["retention"]["note"]
    assert body["next_cursor"]
    second = envelope_ok(
        get(
            app,
            "/api/v1/events",
            f"series_id={SERIES_ID}&limit=2&cursor={body['next_cursor']}",
        )
    )
    assert [item["kind"] for item in second["items"]] == ["gap_closed", "new_series"]
    assert second["next_cursor"] is None


def test_events_limit_bounds(db):
    app = app_for(db)
    for query in ("limit=0", "limit=101"):
        response = get(app, "/api/v1/events", f"series_id={SERIES_ID}&{query}")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# read capacity (spec D3)
# --------------------------------------------------------------------------- #
def test_four_reads_are_served_and_the_fifth_is_refused_without_queueing(
    db, monkeypatch
):
    import asyncio
    import threading

    started = threading.Semaphore(0)
    release = threading.Event()
    real = queries.read_status

    def slow_status(**kwargs):
        started.release()
        assert release.wait(timeout=5)
        return real(**kwargs)

    monkeypatch.setattr(queries, "read_status", slow_status)
    app = app_for(db)
    gate = app.state.dashboard.admission
    assert gate.limit == 4

    async def drive():
        runners = [
            asyncio.create_task(call(app, "GET", "/api/v1/status")) for _ in range(4)
        ]
        # Every one of the four reads has entered the database handler, so the
        # gate is provably saturated when the fifth arrives.
        for _ in range(4):
            await asyncio.to_thread(started.acquire)
        fifth = await call(app, "GET", "/api/v1/status")
        release.set()
        return await asyncio.gather(*runners), fifth

    four, fifth = asyncio.run(drive())
    assert [response.status_code for response in four] == [200, 200, 200, 200]
    assert fifth.status_code == 503
    assert fifth.headers["cache-control"] == "no-store"
    assert fifth.json()["error"]["code"] == "read_capacity_exceeded"
    assert gate.in_use == 0
    # The gate is not stuck: a later request succeeds.
    assert get(app, "/api/v1/status").status_code == 200


def test_admission_gate_is_non_waiting():
    gate = ReadAdmission(limit=4)
    assert all(gate.try_acquire() for _ in range(4))
    assert gate.try_acquire() is False
    gate.release()
    assert gate.try_acquire() is True


def test_cancellation_releases_the_slot(db):
    import asyncio

    app = app_for(db)
    gate = app.state.dashboard.admission

    async def drive():
        task = asyncio.create_task(call(app, "GET", "/api/v1/status"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return gate.in_use

    assert asyncio.run(drive()) == 0


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_openapi_documents_every_bound_route_with_the_frozen_envelope(db):
    app = app_for(db)
    spec = app.openapi()
    assert sorted(spec["paths"]) == [
        "/api/v1/events",
        "/api/v1/history",
        "/api/v1/live",
        "/api/v1/series",
        "/api/v1/status",
        "/api/v1/summary",
        "/health/live",
        "/health/ready",
    ]
    schemas = spec["components"]["schemas"]
    named = {
        "StatusResponse",
        "SeriesListResponse",
        "SummaryResponse",
        "HistoryResponse",
        "EventsResponse",
        "LivenessResponse",
        "ReadinessResponse",
        "ErrorResponse",
        "LiveResponse",
    }
    assert named <= set(schemas)
    for name in named:
        props = schemas[name]["properties"]
        assert props["schema_version"]["default"] == 1
        assert "generated_at" in props
    assert "tokens" in schemas["CounterBlock"]["properties"]
    history = spec["paths"]["/api/v1/history"]["get"]
    assert {param["name"] for param in history["parameters"]} == {
        "series_id",
        "from",
        "to",
        "bucket",
    }
    assert json.dumps(spec).count("HTTPValidationError") == 0
    for path in (
        "/api/v1/status",
        "/api/v1/series",
        "/api/v1/summary",
        "/api/v1/history",
        "/api/v1/events",
    ):
        responses = spec["paths"][path]["get"]["responses"]
        assert {"200", "400", "404", "500", "503"} <= set(responses), path
        assert responses["400"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "ErrorResponse"
        )


def test_no_wildcard_cors_is_configured(db):
    app = app_for(db)
    response = get(app, "/api/v1/status")
    assert "access-control-allow-origin" not in response.headers
    middleware = [layer.cls.__name__ for layer in app.user_middleware]
    assert "CORSMiddleware" not in middleware


# --------------------------------------------------------------------------- #
# live snapshot (live-panel plan Step 3)
# --------------------------------------------------------------------------- #
LIVE_SNAPSHOT = {
    "state": "live",
    "observed_at": "2026-10-27T11:59:59.812Z",
    "requests": {"active": 3, "queued": 1},
    "prefill_tokens_per_second": 1240.4,
    "decode_tokens_per_second": None,
    "decode_tokens_per_second_per_request": None,
    "mtp_acceptance": {
        "window_5s": 0.7,
        "window_1m": None,
        "window_30m": None,
        "lifetime": 0.66,
    },
    "moe_gpu_hit": {"window_5s": 0.93, "window_1m": 0.91, "window_30m": None},
}


class StubMonitor:
    """Stands in for the live poller: publishes one snapshot, no threads."""

    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> dict:
        return self._snapshot


def live_app(db: Path, monitor=None):
    config = make_config(db)
    return create_app(AppState(config, make_health(), db, live=monitor))


def test_live_reports_unavailable_when_no_monitor_is_wired(db):
    body = envelope_ok(get(live_app(db), "/api/v1/live"))
    assert body["state"] == "unavailable"
    assert body["observed_at"] is None
    assert body["requests"] == {"active": None, "queued": None}
    assert body["mtp_acceptance"] == {
        "window_5s": None,
        "window_1m": None,
        "window_30m": None,
        "lifetime": None,
    }
    assert body["moe_gpu_hit"] == {"window_5s": None, "window_1m": None, "window_30m": None}


def test_live_passes_the_snapshot_through_and_keeps_every_null(db):
    body = envelope_ok(get(live_app(db, StubMonitor(LIVE_SNAPSHOT)), "/api/v1/live"))
    assert body["state"] == "live"
    assert body["observed_at"] == "2026-10-27T11:59:59.812Z"
    assert body["requests"] == {"active": 3, "queued": 1}
    assert body["prefill_tokens_per_second"] == 1240.4
    # A missing rate stays null: 0 would read as "idle" in the browser.
    assert body["decode_tokens_per_second"] is None
    assert body["decode_tokens_per_second_per_request"] is None
    assert body["mtp_acceptance"]["window_1m"] is None
    assert body["mtp_acceptance"]["lifetime"] == 0.66
    assert body["moe_gpu_hit"]["window_30m"] is None


def test_live_answers_while_the_read_admission_gate_is_saturated(db):
    app = live_app(db, StubMonitor(LIVE_SNAPSHOT))
    gate = app.state.dashboard.admission
    assert gate.try_acquire() and gate.try_acquire()
    assert gate.try_acquire() and gate.try_acquire()  # capacity = 4, all held
    refused = get(app, "/api/v1/status")
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "read_capacity_exceeded"
    assert get(app, "/api/v1/live").status_code == 200


def test_live_opens_documented_responses_without_the_read_codes(db):
    spec = live_app(db).openapi()
    responses = spec["paths"]["/api/v1/live"]["get"]["responses"]
    assert {"200", "500"} <= set(responses)
    assert "503" not in responses


def test_root_is_a_plain_404_until_the_delivery_3_ui(db):
    # The API application alone has no page route; the built UI is served by the
    # outer ASGI entry point (see tests/test_static.py).
    response = get(app_for(db), "/")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert envelope_ok(response)
