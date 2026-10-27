"""Behavioural checks for the read-only snapshot queries (Step 4).

Every scenario is derived from the Delivery 2 contract (spec D4/D6 and
docs/acceptance-scenarios.md), not from the implementation: exact decimal totals,
null-vs-zero separation, generated empty buckets, half-open ranges, per-scope gap
seconds, retained coverage metadata, and stable keyset pagination.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_storage_helpers import (
    IDENT,
    SOURCE,
    T0,
    apply_seed,
    seed_chain,
)

from jarvis_dashboard import queries, storage
from jarvis_dashboard.accounting import transition_series
from jarvis_dashboard.config import CostRates
from jarvis_dashboard.domain import (
    CACHED_INPUT,
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    INT64_MAX,
    REQUEST_INPUT,
    REQUEST_OUTPUT,
    ScrapeCounters,
    StatsEvidence,
)
from jarvis_dashboard.lifecycle import RuntimeHealth
from jarvis_dashboard.queries import QueryError, encode_series_id

SERIES_ID = encode_series_id(SOURCE, IDENT)
NOW = T0 + timedelta(minutes=6)


def health() -> RuntimeHealth:
    state = RuntimeHealth(poll_interval_seconds=15)
    state.mark_started(T0)
    state.mark_writer_ready()
    state.mark_primary_success(T0)
    return state


def snapshot(at: datetime = NOW) -> object:
    state = health()
    state.mark_primary_success(at)
    return state.snapshot(at)


@pytest.fixture
def conn(tmp_path: Path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    apply_seed(db)
    yield db
    db.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "d.sqlite3"


# --------------------------------------------------------------------------- #
# identifiers, times, ranges
# --------------------------------------------------------------------------- #
def test_series_id_round_trips_and_rejects_malformed_values():
    assert queries.decode_series_id(SERIES_ID) == (SOURCE, IDENT)
    bad = [
        "nonsense",
        "v2." + SERIES_ID.split(".")[1],
        "v1.###not-base64###",
        "v1." + _b64(json.dumps(["s1"]).encode()),
        "v1." + _b64(json.dumps(["s1", 7]).encode()),
        "v1." + _b64(json.dumps(["s1", '{"model_name":"m","engine":"0"}']).encode()),
        "v1." + _b64(b"\xff"),
    ]
    for value in bad:
        with pytest.raises(QueryError) as raised:
            queries.decode_series_id(value)
        assert raised.value.code == "invalid_series_id", value


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_timestamp_parsing_accepts_utc_only():
    assert queries.parse_utc("2026-01-15T10:00:00Z") == T0
    assert queries.parse_utc("2026-01-15T10:00:00+00:00") == T0
    for value in ("2026-01-15T10:00:00", "2026-01-15T10:00:00+02:00", "yesterday", ""):
        with pytest.raises(QueryError) as raised:
            queries.parse_utc(value)
        assert raised.value.code == "invalid_time", value


def test_range_requires_alignment_order_and_bounded_size():
    start, end, seconds = queries.parse_range("2026-01-15T10:00:00Z", "2026-01-15T11:00:00Z", "hour")
    assert (start, end, seconds) == (T0, T0 + timedelta(hours=1), 3600)
    cases = [
        ("2026-01-15T10:30:00Z", "2026-01-15T11:00:00Z", "hour", "unaligned_range"),
        ("2026-01-15T11:00:00Z", "2026-01-15T10:00:00Z", "hour", "invalid_range"),
        ("2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z", "hour", "invalid_range"),
        (
            "2026-01-01T00:00:00Z",
            "2026-02-01T01:00:00Z",
            "hour",
            "range_too_large",
        ),  # 744 hourly buckets is the maximum, 745 is not
        ("2025-01-01T00:00:00Z", "2026-01-03T00:00:00Z", "day", "range_too_large"),
        ("2026-01-15T10:00:00Z", "2026-01-16T00:00:00Z", "week", "invalid_bucket"),
    ]
    # Exactly 744 hourly buckets and 366 daily buckets are accepted.
    assert queries.parse_range("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "hour")[1] == datetime(
        2026, 2, 1, tzinfo=UTC
    )
    assert queries.parse_range("2025-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "day")[1] == datetime(
        2026, 1, 2, tzinfo=UTC
    )
    for raw_from, raw_to, bucket, code in cases:
        with pytest.raises(QueryError) as raised:
            queries.parse_range(raw_from, raw_to, bucket)
        assert raised.value.code == code, (raw_from, raw_to, bucket)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def test_status_reports_activity_without_endpoint_details(db_path, conn):
    # The seed's own polls are the successes at 10:00 and 10:03; the failure is
    # recorded last so the newest attempt is the timeout.
    storage.record_source_attempt(
        conn, SOURCE, "fp1", T0 + timedelta(minutes=4), "upstream_timeout"
    )
    storage.set_runtime_state(conn, storage.RUNTIME_LAST_POLL_AT, T0.isoformat())
    storage.set_runtime_state(conn, storage.RUNTIME_LAST_ERROR_CODE, "upstream_timeout")
    payload = queries.read_status(
        db_path,
        source_id=SOURCE,
        poll_interval_seconds=15,
        raw_retention_days=30,
        health=snapshot(),
        now=NOW,
    )
    assert payload["source_id"] == SOURCE
    assert payload["poll_interval_seconds"] == 15
    assert payload["raw_retention_days"] == 30
    assert payload["first_observation_at"] == T0.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert payload["last_success_at"] == (T0 + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert payload["last_attempt_at"] == (T0 + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert payload["last_error_code"] == "upstream_timeout"
    assert payload["last_poll_at"] == T0.isoformat()
    assert payload["last_recorded_error_code"] == "upstream_timeout"
    assert payload["series_count"] == 1
    assert payload["storage_status"] == "ok"
    assert payload["collector_status"] == "healthy"
    text = json.dumps(payload)
    assert "8000" not in text and "http" not in text and "fingerprint" not in text


def test_status_of_an_unreadable_database_is_a_storage_failure(tmp_path):
    with pytest.raises(QueryError) as raised:
        queries.read_status(
            tmp_path / "missing.sqlite3",
            source_id=SOURCE,
            poll_interval_seconds=15,
            raw_retention_days=30,
            health=snapshot(),
            now=NOW,
        )
    assert (raised.value.code, raised.value.status) == ("storage_unavailable", 503)


# --------------------------------------------------------------------------- #
# series listing and pagination
# --------------------------------------------------------------------------- #
def _three_series(tmp_path: Path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    for index in range(3):
        seed_chain(
            db,
            f"m{index}",
            [(1000 + 100 * i, 100 + 10 * i) for i in range(2)],
            T0 + timedelta(hours=index),
        )
    return db


def test_series_page_is_bounded_and_keyset_stable(tmp_path):
    conn = _three_series(tmp_path)
    path = tmp_path / "d.sqlite3"
    page_now = T0 + timedelta(seconds=20)
    first = queries.read_series_page(
        path,
        source_id=SOURCE,
        limit=2,
        cursor=None,
        now=page_now,
        poll_interval_seconds=15,
        health=snapshot(page_now),
    )
    assert len(first["items"]) == 2
    assert first["next_cursor"]
    names = [item["model_name"] for item in first["items"]]
    item = first["items"][0]
    assert item["labels"] == {"model_name": names[0], "engine": "0"}
    assert item["state"] == "active"
    assert item["first_seen_at"] and item["last_seen_at"]
    assert item["first_valid_at"] and item["last_valid_at"]
    assert "stale_since" in item

    # A row inserted after the first page must not appear on later pages.
    seed_chain(conn, "m-late", [(10, 1), (20, 2)], T0 + timedelta(hours=5))
    second = queries.read_series_page(
        path,
        source_id=SOURCE,
        limit=2,
        cursor=first["next_cursor"],
        now=page_now,
        poll_interval_seconds=15,
        health=snapshot(page_now),
    )
    assert [item["model_name"] for item in second["items"]] == ["m2"]
    assert second["next_cursor"] is None
    assert names + [item["model_name"] for item in second["items"]] == [
        "m0",
        "m1",
        "m2",
    ]


def test_series_page_marks_unobserved_series_stale(tmp_path):
    conn = _three_series(tmp_path)
    path = tmp_path / "d.sqlite3"
    page = queries.read_series_page(
        path,
        source_id=SOURCE,
        limit=10,
        cursor=None,
        now=T0 + timedelta(hours=9),
        poll_interval_seconds=15,
        health=snapshot(),
    )
    assert [item["state"] for item in page["items"]] == ["stale", "stale", "stale"]
    assert all(item["stale_since"] for item in page["items"])


def test_cursors_are_bound_to_their_route_filter_and_source(tmp_path):
    conn = _three_series(tmp_path)
    path = tmp_path / "d.sqlite3"
    page = queries.read_series_page(
        path,
        source_id=SOURCE,
        limit=1,
        cursor=None,
        now=NOW,
        poll_interval_seconds=15,
        health=snapshot(),
    )
    cursor = page["next_cursor"]
    with pytest.raises(QueryError) as raised:
        queries.read_series_page(
            path,
            source_id="other",
            limit=1,
            cursor=cursor,
            now=NOW,
            poll_interval_seconds=15,
            health=snapshot(),
        )
    assert raised.value.code == "invalid_cursor"
    with pytest.raises(QueryError) as raised:
        queries.read_events(
            path,
            source_id=SOURCE,
            series_id=encode_series_id(SOURCE, _ident("m0")),
            limit=5,
            cursor=cursor,
            now=NOW,
            raw_retention_days=30,
        )
    assert raised.value.code == "invalid_cursor"
    for tampered in ("v1.", cursor[:-3] + "AAA", "v9.abcdefgh"):
        with pytest.raises(QueryError) as raised:
            queries.read_series_page(
                path,
                source_id=SOURCE,
                limit=1,
                cursor=tampered,
                now=NOW,
                poll_interval_seconds=15,
                health=snapshot(),
            )
        assert raised.value.code == "invalid_cursor", tampered


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #
def _a2(tmp_path: Path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    seed_chain(db, "m", [(1000, 100), (1600, 140), (1900, 160)], T0)
    return db


def test_summary_lifetime_matches_the_acceptance_numbers(tmp_path):
    conn = _a2(tmp_path)
    payload = queries.read_summary(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        epoch_limit=5,
        epoch_cursor=None,
        health=snapshot(T0 + timedelta(seconds=45)),
        now=T0 + timedelta(seconds=45),
        poll_interval_seconds=15,
    )
    lifetime = payload["ranges"]["lifetime"]
    assert lifetime["counters"][COMPUTED_INPUT]["tokens"] == "900"
    assert lifetime["counters"][GENERATED_OUTPUT]["tokens"] == "60"
    assert lifetime["processed_total"] == "960"
    # Optional counters are counted on their own, never merged into the primary.
    assert lifetime["counters"][REQUEST_INPUT]["tokens"] == "2"
    assert lifetime["counters"][REQUEST_OUTPUT]["tokens"] == "2"
    assert lifetime["started_at"].endswith("T00:00:00.000000Z") is False
    today = payload["ranges"]["today"]
    assert today["started_at"] == "2026-01-15T00:00:00.000000Z"
    assert today["counters"][COMPUTED_INPUT]["tokens"] == "900"
    assert payload["ranges"]["month"]["counters"][COMPUTED_INPUT]["tokens"] == "900"
    assert payload["epoch"]["reason"] == "new_series"
    assert payload["epochs"]["items"][0]["opening_computed_input"] == "1000"
    assert payload["collection_status"] == "healthy"
    conn.close()


def test_summary_rates_use_the_latest_accepted_interval(tmp_path):
    _a2(tmp_path)
    payload = queries.read_summary(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        epoch_limit=5,
        epoch_cursor=None,
        health=snapshot(T0 + timedelta(seconds=45)),
        now=T0 + timedelta(seconds=45),
        poll_interval_seconds=15,
    )
    rate = payload["rates"][COMPUTED_INPUT]
    assert rate["tokens"] == "300"
    assert rate["interval_seconds"] == 15.0
    assert rate["tokens_per_second"] == 20.0
    assert rate["eligible"] is True
    # A baseline-only series has no accepted interval yet.
    db = storage.open_storage(tmp_path / "empty.sqlite3")
    seed_chain(db, "solo", [(500, 50)], T0)
    db.close()
    solo = queries.read_summary(
        tmp_path / "empty.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("solo")),
        epoch_limit=5,
        epoch_cursor=None,
        health=snapshot(),
        now=T0 + timedelta(seconds=15),
        poll_interval_seconds=15,
    )
    assert solo["rates"][COMPUTED_INPUT]["tokens_per_second"] is None
    # A baseline is not a zero: nothing was observed, so the value stays null.
    assert solo["ranges"]["lifetime"]["counters"][COMPUTED_INPUT]["tokens"] is None
    assert solo["ranges"]["lifetime"]["counters"][COMPUTED_INPUT]["observed_intervals"] == 0
    assert solo["ranges"]["lifetime"]["processed_total"] is None


def test_summary_distinguishes_unobserved_from_a_real_zero(tmp_path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    seed_chain(db, "flat", [(1000, 100), (1000, 100)], T0)  # unchanged work
    db.close()
    payload = queries.read_summary(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("flat")),
        epoch_limit=5,
        epoch_cursor=None,
        health=snapshot(),
        now=T0 + timedelta(seconds=30),
        poll_interval_seconds=15,
    )
    counters = payload["ranges"]["lifetime"]["counters"]
    assert counters[COMPUTED_INPUT]["tokens"] == "0"
    assert counters[COMPUTED_INPUT]["observed_intervals"] == 1
    assert counters[COMPUTED_INPUT]["quality_flags"] == []
    assert payload["ranges"]["lifetime"]["processed_total"] == "0"
    assert counters[REQUEST_INPUT]["observed_intervals"] == 1  # optional counters move


def test_summary_reports_unknown_and_malformed_series(tmp_path):
    _a2(tmp_path)
    path = tmp_path / "d.sqlite3"
    with pytest.raises(QueryError) as raised:
        queries.read_summary(
            path,
            source_id=SOURCE,
            series_id=encode_series_id(SOURCE, _ident("nobody")),
            epoch_limit=5,
            epoch_cursor=None,
            health=snapshot(),
            now=NOW,
            poll_interval_seconds=15,
        )
    assert (raised.value.code, raised.value.status) == ("series_not_found", 404)
    with pytest.raises(QueryError) as raised:
        queries.read_summary(
            path,
            source_id=SOURCE,
            series_id="v1.nope",
            epoch_limit=5,
            epoch_cursor=None,
            health=snapshot(),
            now=NOW,
            poll_interval_seconds=15,
        )
    assert raised.value.code == "invalid_series_id"


def test_epoch_page_is_bounded_newest_first_and_bound_to_the_series(tmp_path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    for index in range(25):
        seed_chain(db, "m", [(1000 + 10 * index, 100 + index)], T0 + timedelta(minutes=index))
    db.close()
    path = tmp_path / "d.sqlite3"
    series_id = encode_series_id(SOURCE, _ident("m"))
    page = queries.read_summary(
        path,
        source_id=SOURCE,
        series_id=series_id,
        epoch_limit=50,
        epoch_cursor=None,
        health=snapshot(),
        now=NOW,
        poll_interval_seconds=15,
    )
    items = page["epochs"]["items"]
    assert len(items) == queries.MAX_EPOCH_PAGE
    assert [item["epoch_id"] for item in items] == sorted(
        (item["epoch_id"] for item in items), reverse=True
    )
    assert page["epochs"]["next_cursor"]
    second = queries.read_summary(
        path,
        source_id=SOURCE,
        series_id=series_id,
        epoch_limit=50,
        epoch_cursor=page["epochs"]["next_cursor"],
        health=snapshot(),
        now=NOW,
        poll_interval_seconds=15,
    )
    ids = {item["epoch_id"] for item in page["epochs"]["items"]}
    assert ids.isdisjoint({item["epoch_id"] for item in second["epochs"]["items"]})
    other = storage.open_storage(path)
    seed_chain(other, "other", [(1000, 100)], T0)
    other.close()
    with pytest.raises(QueryError) as raised:
        queries.read_summary(
            path,
            source_id=SOURCE,
            series_id=encode_series_id(SOURCE, _ident("other")),
            epoch_limit=5,
            epoch_cursor=page["epochs"]["next_cursor"],
            health=snapshot(),
            now=NOW,
            poll_interval_seconds=15,
        )
    assert raised.value.code == "invalid_cursor"
    with pytest.raises(QueryError) as raised:
        queries.read_summary(
            path,
            source_id=SOURCE,
            series_id=series_id,
            epoch_limit=5,
            epoch_cursor="v1.not-a-cursor",
            health=snapshot(),
            now=NOW,
            poll_interval_seconds=15,
        )
    assert raised.value.code == "invalid_cursor"


def _ident(model: str) -> str:
    from jarvis_dashboard.domain import canonical_labels_json

    return canonical_labels_json({"model_name": model, "engine": "0"})


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #
def test_history_generates_every_bucket_and_never_clamps(tmp_path):
    _a2(tmp_path)
    payload = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        started_at=T0,
        ended_at=T0 + timedelta(hours=3),
        bucket="hour",
        health=snapshot(),
        now=T0 + timedelta(hours=4),
        poll_interval_seconds=15,
    )
    assert payload["bucket_count"] == 3
    assert [b["bucket"] for b in payload["buckets"]] == [
        "2026-01-15T10Z",
        "2026-01-15T11Z",
        "2026-01-15T12Z",
    ]
    first, empty = payload["buckets"][0], payload["buckets"][1]
    assert first["counters"][COMPUTED_INPUT]["tokens"] == "900"
    assert first["counters"][COMPUTED_INPUT]["observed_intervals"] == 2
    assert first["processed_total"] == "960"
    assert first["incomplete"] is False
    # The chain stops at 10:00:30 and the request is four hours later, so the
    # trailing hour is a real collection gap, not a clean bucket.
    assert first["partial"] is True
    assert first["counters"][COMPUTED_INPUT]["first_observed_at"].endswith("000000Z")
    assert empty["counters"][COMPUTED_INPUT]["tokens"] is None
    assert empty["counters"][COMPUTED_INPUT]["observed_intervals"] == 0
    assert empty["counters"][COMPUTED_INPUT]["first_observed_at"] is None
    assert payload["range"]["counters"][COMPUTED_INPUT]["tokens"] == "900"
    assert payload["range"]["processed_total"] == "960"


def test_history_day_buckets_aggregate_the_same_rows(tmp_path):
    _a2(tmp_path)
    payload = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        started_at=datetime(2026, 1, 15, tzinfo=UTC),
        ended_at=datetime(2026, 1, 17, tzinfo=UTC),
        bucket="day",
        health=snapshot(),
        now=datetime(2026, 1, 17, tzinfo=UTC),
        poll_interval_seconds=15,
    )
    assert [b["bucket"] for b in payload["buckets"]] == ["2026-01-15", "2026-01-16"]
    assert payload["buckets"][0]["counters"][COMPUTED_INPUT]["tokens"] == "900"
    assert payload["buckets"][1]["counters"][COMPUTED_INPUT]["tokens"] is None


def test_history_excludes_the_ending_bound_exactly(tmp_path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    seed_chain(
        db,
        "m",
        [(1000, 100), (1100, 110), (1200, 120)],
        T0,
        times=[T0, T0 + timedelta(minutes=10), T0 + timedelta(minutes=20)],
    )
    db.close()
    # A range that does not align to the bucket boundary is refused, not clamped.
    with pytest.raises(QueryError) as raised:
        queries.parse_range("2026-01-15T10:00:00Z", "2026-01-15T10:15:00Z", "hour")
    assert raised.value.code == "unaligned_range"
    whole = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        bucket="hour",
        health=snapshot(),
        now=T0 + timedelta(hours=2),
        poll_interval_seconds=15,
    )
    # Both intervals end inside [10:00, 11:00): 100 + 100 = 200 tokens.
    assert whole["buckets"][0]["counters"][COMPUTED_INPUT]["tokens"] == "200"
    assert whole["range"]["counters"][COMPUTED_INPUT]["tokens"] == "200"


def test_history_reports_gap_seconds_from_authoritative_spans(tmp_path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    # 10:00 -> 12:04 outage: two full hours plus 240 s of recovered movement.
    times = [T0, T0 + timedelta(hours=2, minutes=4)]
    seed_chain(db, "m", [(100, 10), (400, 40)], T0, times=times)
    db.close()
    payload = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        started_at=T0,
        ended_at=T0 + timedelta(hours=3),
        bucket="hour",
        health=snapshot(),
        # Inside two intervals of the 12:04 recovery: only recorded spans apply.
        now=T0 + timedelta(hours=2, minutes=4, seconds=20),
        poll_interval_seconds=15,
    )
    hours = {b["bucket"]: b for b in payload["buckets"]}
    # The span starts at the last accepted observation (10:00), so hour 10 is
    # entirely unobserved; the recovered hour keeps only its trailing 240 s.
    assert hours["2026-01-15T10Z"]["gap_seconds"]["primary"] == 3600.0
    outage = hours["2026-01-15T11Z"]
    assert outage["gap_seconds"]["primary"] == 3600.0
    assert outage["counters"][COMPUTED_INPUT]["gap_seconds"] == 3600.0
    assert outage["counters"][GENERATED_OUTPUT]["gap_seconds"] == 3600.0  # coupled scope
    assert outage["counters"][REQUEST_INPUT]["gap_seconds"] == 3600.0
    assert outage["counters"][COMPUTED_INPUT]["tokens"] is None
    assert "collection_gap" in outage["quality_flags"]
    assert outage["partial"] is True
    assert hours["2026-01-15T12Z"]["counters"][COMPUTED_INPUT]["tokens"] == "300"
    assert hours["2026-01-15T12Z"]["gap_seconds"]["primary"] == pytest.approx(240.0)
    # The range total is the span itself: 10:00 -> 12:04.
    assert payload["range"]["gap_seconds"]["primary"] == pytest.approx(7440.0)
    assert payload["range"]["partial"] is True


def test_history_overlays_implicit_downtime_beyond_two_intervals(tmp_path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    seed_chain(db, "m", [(100, 10)], T0)
    db.close()
    now = T0 + timedelta(minutes=30)
    payload = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        started_at=T0,
        ended_at=now,
        bucket="hour",
        health=snapshot(),
        now=now,
        poll_interval_seconds=15,
    )
    first = payload["buckets"][0]
    # Downtime starts *after* two 15 s intervals (10:00:30) and is only counted
    # up to the request instant (10:30), so this bucket reports 1770 s.
    assert first["gap_seconds"]["primary"] == pytest.approx(1770)
    assert first["partial"] is True
    assert first["incomplete"] is True
    assert first["counters"][COMPUTED_INPUT]["gap_seconds"] == pytest.approx(1770)
    # Within two intervals of the last success there is no implicit gap.
    close = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        bucket="hour",
        health=snapshot(),
        now=T0 + timedelta(seconds=25),
        poll_interval_seconds=15,
    )
    assert close["buckets"][0]["gap_seconds"] == {
        "primary": 0.0,
        "request_input": 0.0,
        "request_output": 0.0,
        "cached_input": 0.0,
    }


def test_history_surfaces_incomplete_legacy_coverage(tmp_path, conn):
    conn.execute(
        "UPDATE hourly_totals SET metadata_complete = 0, first_observed_at = NULL,"
        " last_observed_at = NULL"
    )
    conn.commit()
    payload = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=SERIES_ID,
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        bucket="hour",
        health=snapshot(),
        now=T0 + timedelta(hours=1),
        poll_interval_seconds=15,
    )
    bucket = payload["buckets"][0]
    assert bucket["counters"][COMPUTED_INPUT]["tokens"] == "300"
    assert bucket["metadata_complete"] is False
    assert bucket["counters"][COMPUTED_INPUT]["first_observed_at"] is None
    assert bucket["partial"] is True
    assert payload["range"]["metadata_complete"] is False


def test_history_reports_overflow_instead_of_wrapping(tmp_path, conn):
    conn.execute(
        "INSERT INTO hourly_totals (source_id, identity, hour, counter_key, tokens,"
        " observed_intervals, quality_json, metadata_complete)"
        " VALUES (?, ?, '2026-01-15T11Z', ?, ?, 1, '[]', 1)",
        (SOURCE, IDENT, COMPUTED_INPUT, INT64_MAX),
    )
    conn.commit()
    with pytest.raises(QueryError) as raised:
        queries.read_history(
            tmp_path / "d.sqlite3",
            source_id=SOURCE,
            series_id=SERIES_ID,
            started_at=T0,
            ended_at=T0 + timedelta(hours=2),
            bucket="hour",
            health=snapshot(),
            now=T0 + timedelta(hours=2),
            poll_interval_seconds=15,
        )
    assert (raised.value.code, raised.value.status) == ("counter_overflow", 500)


def test_history_marks_the_open_bucket_incomplete(tmp_path):
    _a2(tmp_path)
    payload = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        started_at=T0,
        ended_at=T0 + timedelta(hours=2),
        bucket="hour",
        health=snapshot(),
        now=T0 + timedelta(minutes=90),
        poll_interval_seconds=15,
    )
    assert payload["buckets"][0]["incomplete"] is False
    assert payload["buckets"][1]["incomplete"] is True
    assert payload["range"]["partial"] is True


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def test_events_page_is_descending_inclusive_and_stable(tmp_path):
    db = storage.open_storage(tmp_path / "d.sqlite3")
    seed_chain(db, "m", [(1000, 100), (1100, 110)], T0)
    def iso(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    db.execute(
        "INSERT INTO events (source_id, identity, kind, occurred_at, details_json)"
        " VALUES (?, NULL, 'source_wide', ?, '{}')",
        (SOURCE, iso(T0 + timedelta(seconds=30))),
    )
    db.execute(
        "INSERT INTO events (source_id, identity, kind, occurred_at, details_json)"
        " VALUES (?, ?, 'gap_closed', ?, '{\"scope\":\"primary\"}')",
        (SOURCE, _ident("m"), iso(T0 + timedelta(seconds=45))),
    )
    db.execute(
        "INSERT INTO events (source_id, identity, kind, occurred_at, details_json)"
        " VALUES (?, ?, 'reset_boundary', ?, '{}')",
        (SOURCE, _ident("m"), iso(T0 + timedelta(seconds=50))),
    )
    db.commit()
    db.close()
    path = tmp_path / "d.sqlite3"
    page = queries.read_events(
        path,
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        limit=2,
        cursor=None,
        now=NOW,
        raw_retention_days=30,
    )
    kinds = [item["kind"] for item in page["items"]]
    assert len(kinds) == 2
    assert page["items"][0]["occurred_at"] >= page["items"][1]["occurred_at"]
    assert page["next_cursor"]
    assert page["retention"]["raw_retention_days"] == 30
    assert page["retention"]["raw_samples_until"]

    # A newer event inserted after the first page must not leak into page two.
    later = storage.open_storage(path)
    later.execute(
        "INSERT INTO events (source_id, identity, kind, occurred_at, details_json)"
        " VALUES (?, NULL, 'newer', ?, '{}')",
        (SOURCE, (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")),
    )
    later.commit()
    later.close()
    second = queries.read_events(
        path,
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("m")),
        limit=2,
        cursor=page["next_cursor"],
        now=NOW,
        raw_retention_days=30,
    )
    assert "newer" not in [item["kind"] for item in second["items"]]
    assert "source_wide" in [item["kind"] for item in second["items"]]  # included, page 2


# --------------------------------------------------------------------------- #
# API-equivalent cost estimate (docs/cost-estimate-implementation-plan.md D4)
# --------------------------------------------------------------------------- #
RATES = CostRates(
    input_per_mtok=Decimal("3"),
    cached_input_per_mtok=Decimal("0.30"),
    output_per_mtok=Decimal("15"),
)


def _polls(db, model: str, samples, poll: int = 15) -> None:
    """Write exact per-poll counters: (at, ci, go, ri, ro, ca-or-None)."""
    labels = {"model_name": model, "engine": "0"}
    state = None
    for index, (at, ci, go, ri, ro, ca) in enumerate(samples):
        values = {"computed_input": ci, "generated_output": go}
        if ri is not None:
            values["request_input"] = ri
        if ro is not None:
            values["request_output"] = ro
        if ca is not None:
            values["cached_input"] = ca
        plan = transition_series(
            state,
            ScrapeCounters(labels, values, {}),
            at,
            StatsEvidence(model, 1000 + (at - T0).total_seconds(), at, at),
            poll,
        )
        storage.apply_poll(
            db,
            source_id=SOURCE,
            endpoint_fingerprint="fp1",
            poll_id=f"cost-{model}-{index}",
            observed_at=at,
            plans=[plan],
        )
        state = plan.state


def _cost_series(
    tmp_path: Path,
    model: str = "cost",
    *,
    cached_from: int | None = None,
    hours: tuple[int, ...] = (10,),
) -> None:
    """One hour of exact traffic: 1 000 000 input, 600 000 cached, 100 000 output.

    `cached_from` is the first hour publishing the cache counter (None = from the
    baseline; a counter's first observation only sets a baseline, so it is first
    priced the hour after it appears).
    """
    db = storage.open_storage(tmp_path / "d.sqlite3")
    samples = [(T0, 0, 0, 0, 0, 0 if cached_from is None else None)]
    for hour in sorted(hours):
        at = T0 + timedelta(seconds=15) if hour == 10 else T0 + timedelta(hours=hour - 10)
        cached = None if cached_from is not None and hour < cached_from else 600_000 * (hour - 9)
        samples.append(
            (
                at,
                100 * (hour - 9),
                10 * (hour - 9),
                1_000_000 * (hour - 9),
                100_000 * (hour - 9),
                cached,
            )
        )
    _polls(db, model, samples)


def _summary(tmp_path: Path, model: str, rates, now: datetime):
    return queries.read_summary(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident(model)),
        epoch_limit=5,
        epoch_cursor=None,
        health=snapshot(now),
        now=now,
        poll_interval_seconds=15,
        cost_rates=rates,
    )


def test_api_equivalent_prices_a_fully_observed_hour(tmp_path):  # AC1
    _cost_series(tmp_path, "cost", hours=(10,))
    cost = _summary(tmp_path, "cost", RATES, T0 + timedelta(hours=1))["ranges"]["lifetime"][
        "api_equivalent"
    ]
    assert cost["amount"] == "2.880000"  # 400000*3 + 600000*0.30 + 100000*15, / 1e6
    assert cost["currency"] == "USD"
    assert cost["tokens"] == {
        "uncached_input": "400000",
        "cached_input": "600000",
        "output": "100000",
    }
    assert cost["cache_hit_ratio"] == 0.6
    assert cost["priced_hours"] == 1
    assert cost["expected_hours"] == 1
    assert cost["partial"] is False
    assert cost["flags"] == []


def test_api_equivalent_prices_only_hours_with_cache_tracking(tmp_path):  # AC2
    _cost_series(tmp_path, "cost", cached_from=11, hours=(10, 11, 12))
    cost = _summary(tmp_path, "cost", RATES, T0 + timedelta(hours=3))["ranges"]["lifetime"][
        "api_equivalent"
    ]
    # Hours 10-11 request tokens are excluded from the amount AND the cache share.
    assert cost["amount"] == "2.880000"
    assert cost["cache_hit_ratio"] == 0.6
    assert cost["tokens"]["cached_input"] == "600000"
    assert cost["priced_hours"] == 1
    assert cost["expected_hours"] == 3
    assert cost["partial"] is True


def test_api_equivalent_leaves_a_gap_hour_unpriced(tmp_path):  # AC3
    db = storage.open_storage(tmp_path / "d.sqlite3")
    _polls(
        db,
        "cost",
        [
            (T0, 1000, 100, 0, 0, 0),
            (T0 + timedelta(seconds=15), 1001, 101, 1_000_000, 100_000, 600_000),
            # The cache counter vanishes; the request counters keep running.
            (T0 + timedelta(hours=1), 1100, 110, 2_000_000, 200_000, None),
            (T0 + timedelta(hours=1, seconds=15), 1101, 111, 3_000_000, 300_000, None),
        ],
    )
    cost = _summary(tmp_path, "cost", RATES, T0 + timedelta(hours=2))["ranges"]["lifetime"][
        "api_equivalent"
    ]
    assert cost["priced_hours"] == 1
    assert cost["expected_hours"] == 2
    assert cost["partial"] is True
    # The unpriced hour's request tokens are not in the amount or the share.
    assert cost["tokens"] == {
        "uncached_input": "400000",
        "cached_input": "600000",
        "output": "100000",
    }
    assert cost["amount"] == "2.880000"
    # The existing per-scope gap reporting is unchanged by the cost model.
    lifetime = _summary(tmp_path, "cost", RATES, T0 + timedelta(hours=2))["ranges"]["lifetime"]
    assert lifetime["gap_seconds"][CACHED_INPUT] > 0


def test_api_equivalent_flags_cached_input_ahead_of_request(tmp_path):  # AC5
    db = storage.open_storage(tmp_path / "d.sqlite3")
    _polls(
        db,
        "cost",
        [
            (T0, 1000, 100, 0, 0, 0),
            (T0 + timedelta(seconds=15), 1001, 101, 1_000_000, 100_000, 1_200_000),
        ],
    )
    cost = _summary(tmp_path, "cost", RATES, T0 + timedelta(hours=1))["ranges"]["lifetime"][
        "api_equivalent"
    ]
    assert cost["tokens"]["uncached_input"] == "0"
    assert cost["tokens"]["cached_input"] == "1200000"
    assert cost["flags"] == ["cache_exceeds_input"]
    # Uncached priced as zero, never a negative credit: 1.2M*0.30 + 100k*15.
    assert cost["amount"] == "1.860000"
    assert cost["cache_hit_ratio"] == 1.2


def test_api_equivalent_without_rates_keeps_share_and_coverage(tmp_path):  # AC4
    _cost_series(tmp_path, "cost", hours=(10, 11))
    cost = _summary(tmp_path, "cost", None, T0 + timedelta(hours=2))["ranges"]["lifetime"][
        "api_equivalent"
    ]
    assert cost["amount"] is None
    assert cost["cache_hit_ratio"] == 0.6
    assert cost["priced_hours"] == 2
    assert cost["expected_hours"] == 2
    assert cost["partial"] is False


def test_api_equivalent_is_null_when_nothing_is_priced(tmp_path):  # AC8
    _cost_series(tmp_path, "cost", cached_from=99, hours=(10,))
    cost = _summary(tmp_path, "cost", RATES, T0 + timedelta(hours=1))["ranges"]["lifetime"][
        "api_equivalent"
    ]
    assert cost["amount"] is None
    assert cost["cache_hit_ratio"] is None
    assert cost["priced_hours"] == 0
    assert cost["expected_hours"] == 1
    assert cost["partial"] is True
    assert cost["flags"] == []


def test_repeated_identical_polls_never_recredit_the_estimate(tmp_path):  # money integrity
    db = storage.open_storage(tmp_path / "d.sqlite3")
    priced = (T0 + timedelta(seconds=15), 100, 10, 1_000_000, 100_000, 1_200_000)
    _polls(
        db,
        "cost",
        [(T0, 0, 0, 0, 0, 600_000), priced]
        + [
            (priced[0] + timedelta(seconds=15 * (index + 1)), *priced[1:])
            for index in range(10)
        ],
    )
    db.close()
    # Ten unchanged republishes after the priced jump must add nothing: a repeated
    # scrape is a valid zero increment, never a second credit of the same tokens.
    cost = _summary(tmp_path, "cost", RATES, T0 + timedelta(hours=1))["ranges"]["lifetime"][
        "api_equivalent"
    ]
    assert cost["amount"] == "2.880000"
    assert cost["tokens"] == {
        "uncached_input": "400000",
        "cached_input": "600000",
        "output": "100000",
    }
    assert cost["priced_hours"] == 1


def test_expected_hours_ignores_a_current_hour_without_rows(tmp_path):
    _cost_series(tmp_path, "cost", hours=(10,))
    # Mid-hour: the hour containing `now` has rows, so it is expected and priced.
    inside = _summary(tmp_path, "cost", RATES, T0 + timedelta(minutes=30))["ranges"]["lifetime"]
    assert inside["api_equivalent"]["expected_hours"] == 1
    assert inside["api_equivalent"]["partial"] is False
    # Two hours later the trailing hour has no rows: it is not expected yet.
    later = _summary(tmp_path, "cost", RATES, T0 + timedelta(hours=2, minutes=30))[
        "ranges"
    ]["lifetime"]
    assert later["api_equivalent"]["expected_hours"] == 2
    assert later["api_equivalent"]["partial"] is True


def test_rates_reprice_the_same_stored_history(tmp_path):  # AC6
    _cost_series(tmp_path, "cost", hours=(10,))
    now = T0 + timedelta(hours=1)
    cheap = CostRates(Decimal("1"), Decimal("0.10"), Decimal("5"))
    first = _summary(tmp_path, "cost", RATES, now)["ranges"]["lifetime"]["api_equivalent"]
    second = _summary(tmp_path, "cost", cheap, now)["ranges"]["lifetime"]["api_equivalent"]
    assert first["amount"] == "2.880000"
    # 400000*1 + 600000*0.10 + 100000*5 = 960,000 / 1e6
    assert second["amount"] == "0.960000"
    # Money is a read-time function of the rows: nothing monetary is stored.
    tables = {
        row[0]
        for row in storage.open_storage(tmp_path / "d.sqlite3").execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert not [name for name in tables if "cost" in name or "amount" in name]


def test_history_range_carries_cost_but_buckets_do_not(tmp_path):  # D5
    _cost_series(tmp_path, "cost", hours=(10,))
    payload = queries.read_history(
        tmp_path / "d.sqlite3",
        source_id=SOURCE,
        series_id=encode_series_id(SOURCE, _ident("cost")),
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        bucket="hour",
        health=snapshot(T0 + timedelta(hours=1)),
        now=T0 + timedelta(hours=1),
        poll_interval_seconds=15,
        cost_rates=RATES,
    )
    assert payload["range"]["api_equivalent"]["amount"] == "2.880000"
    assert all("api_equivalent" not in bucket for bucket in payload["buckets"])


def test_status_reports_configured_cost_rates(tmp_path):  # D5
    storage.open_storage(tmp_path / "d.sqlite3").close()

    def status(rates):
        return queries.read_status(
            tmp_path / "d.sqlite3",
            source_id=SOURCE,
            poll_interval_seconds=15,
            raw_retention_days=30,
            health=snapshot(),
            now=NOW,
            cost_rates=rates,
        )

    assert status(RATES)["cost_rates"] == {
        "currency": "USD",
        "input_per_mtok": "3",
        "cached_input_per_mtok": "0.30",
        "output_per_mtok": "15",
    }
    assert status(None)["cost_rates"] is None
