"""Live sampling, window engine, and snapshot rules (live-panel plan Step 1).

Distinguishing scenarios: the live field extraction nullifies only the tile a
bad field belongs to (never the whole poll), a structural ``/stats`` failure
reuses the collection's stable codes, the MTP/MoE group is unavailable when a
draft name has zero **or several** samples, every window stays ``None`` until a
full span of continuous samples exists (and within the 2 s tolerance), an idle
denominator is a placeholder and never ``0``, an uptime drop / counter drop /
>3 s gap clears the whole run, and a source silent for more than 3 s goes null
with the snapshot state moving ``starting → live → stale → live``.

The clock is injected: the 30-minute boundary is proven here, never in a browser.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime

import pytest

from jarvis_dashboard.config import Config
from jarvis_dashboard.live import (
    DraftSample,
    LiveMonitor,
    LiveWindowEngine,
    StatsSample,
    parse_draft_document,
    parse_stats_document,
)
from jarvis_dashboard.metrics import UpstreamError

WALL = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)

STATS_BODY = json.dumps(
    {
        "model": "m",
        "uptime_s": 100.0,
        "running": 3,
        "waiting": 1,
        "prefill_tps": 1240.4,
        "decode_tps": 180.2,
        "experts": {"routed": 1000, "routed_res": 930},
    }
).encode()

LABELS = '{engine="0",model_name="m"}'
DRAFT_BODY = (
    f"radiance:draft_tokens_total{LABELS} 1000\n"
    f"radiance:draft_accepted_total{LABELS} 700\n"
)


class Clock:
    """Injected monotonic clock; the engine never reads the real one."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


def stats_sample(**overrides: object) -> StatsSample:
    values: dict[str, object] = {
        "uptime_s": 100.0,
        "running": 3,
        "waiting": 1,
        "prefill_tps": 1240.4,
        "decode_tps": 180.2,
        "routed": 1000,
        "routed_res": 930,
    }
    values.update(overrides)
    return StatsSample(**values)  # type: ignore[arg-type]


def draft_sample(tokens: int, accepted: int) -> DraftSample:
    return DraftSample(draft_tokens=tokens, draft_accepted=accepted)


# --------------------------------------------------------------------------- #
# /stats extraction (plan D3)
# --------------------------------------------------------------------------- #
def test_stats_document_exposes_every_live_field():
    sample = parse_stats_document(STATS_BODY)
    assert sample.uptime_s == 100.0
    assert (sample.running, sample.waiting) == (3, 1)
    assert sample.prefill_tps == pytest.approx(1240.4)
    assert sample.decode_tps == pytest.approx(180.2)
    assert (sample.routed, sample.routed_res) == (1000.0, 930.0)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("running", "3"),
        ("running", -1),
        ("running", True),
        ("running", 1.5),
        ("waiting", None),
        ("waiting", float("nan")),
        ("prefill_tps", "fast"),
        ("prefill_tps", -1.0),
        ("prefill_tps", 1e999),
        ("decode_tps", []),
        ("decode_tps", -0.5),
    ],
)
def test_a_bad_live_field_nullifies_only_its_own_tile(field: str, bad_value: object):
    document = json.loads(STATS_BODY)
    document[field] = bad_value
    sample = parse_stats_document(json.dumps(document).encode())
    assert getattr(sample, field) is None
    # The rest of the poll is still usable: sibling tiles keep their values.
    assert sample.running == (None if field == "running" else 3)
    assert sample.waiting == (None if field == "waiting" else 1)
    assert sample.routed == 1000.0
    assert sample.uptime_s == 100.0


def test_missing_field_nullifies_only_its_own_tile():
    document = json.loads(STATS_BODY)
    del document["waiting"]
    sample = parse_stats_document(json.dumps(document).encode())
    assert sample.waiting is None
    assert sample.running == 3
    assert sample.decode_tps == pytest.approx(180.2)


def test_missing_experts_block_nullifies_only_the_moe_pair():
    document = json.loads(STATS_BODY)
    del document["experts"]
    sample = parse_stats_document(json.dumps(document).encode())
    assert (sample.routed, sample.routed_res) == (None, None)
    assert sample.running == 3
    assert sample.prefill_tps == pytest.approx(1240.4)


@pytest.mark.parametrize(
    "experts",
    [
        {"routed": 1000},
        {"routed_res": 930},
        {"routed": 1000, "routed_res": "930"},
        {"routed": 1000, "routed_res": -1},
        {"routed": 1000, "routed_res": 1e999},
        "930",
        [],
    ],
)
def test_a_malformed_experts_block_nullifies_the_whole_pair(experts: object):
    document = json.loads(STATS_BODY)
    document["experts"] = experts
    sample = parse_stats_document(json.dumps(document).encode())
    assert (sample.routed, sample.routed_res) == (None, None)
    assert sample.running == 3


@pytest.mark.parametrize(
    "body,code",
    [
        (b"not json", "stats_invalid_json"),
        (b"[1, 2]", "stats_invalid_shape"),
        (b'"model"', "stats_invalid_shape"),
        (b'{"model": "m"}', "stats_invalid_uptime"),
        (b'{"model": "m", "uptime_s": "100"}', "stats_invalid_uptime"),
        (b'{"model": "m", "uptime_s": -1}', "stats_invalid_uptime"),
        (b'{"model": "m", "uptime_s": 1e999}', "stats_invalid_uptime"),
    ],
)
def test_structural_stats_failures_reuse_the_collection_codes(body: bytes, code: str):
    with pytest.raises(UpstreamError) as raised:
        parse_stats_document(body)
    assert raised.value.code == code


def test_stats_body_is_not_decoded_as_utf8_text_by_hand():
    # A non-UTF-8 body is a JSON failure, not a crash.
    with pytest.raises(UpstreamError) as raised:
        parse_stats_document(b"\xff\xfe\x00")
    assert raised.value.code == "stats_invalid_json"


# --------------------------------------------------------------------------- #
# /metrics draft pair (plan D3): exactly one sample per name, exact integers
# --------------------------------------------------------------------------- #
def test_draft_pair_is_read_as_exact_integers():
    sample = parse_draft_document(DRAFT_BODY)
    assert (sample.draft_tokens, sample.draft_accepted) == (1000, 700)


def test_draft_pair_ignores_unrelated_families():
    text = (
        "# TYPE radiance:prefill_tokens_total counter\n"
        f"radiance:prefill_tokens_total{LABELS} 4000\n"
        "vllm:prompt_tokens_total{engine=\"0\"} 12\n" + DRAFT_BODY
    )
    sample = parse_draft_document(text)
    assert (sample.draft_tokens, sample.draft_accepted) == (1000, 700)


@pytest.mark.parametrize(
    "text",
    [
        "",  # nothing at all
        f"radiance:draft_tokens_total{LABELS} 1000\n",  # accepted missing
        f"radiance:draft_accepted_total{LABELS} 700\n",  # tokens missing
        DRAFT_BODY + f"radiance:draft_tokens_total{{engine=\"1\"}} 50\n",  # 2 label sets
        DRAFT_BODY + f"radiance:draft_tokens_total{LABELS} 60\n",  # duplicated sample
        DRAFT_BODY.replace(" 700", " 70.5"),  # fractional lexeme
        DRAFT_BODY.replace(" 700", " -700"),  # negative
        DRAFT_BODY.replace(" 700", " NaN"),  # non-finite
        DRAFT_BODY.replace(" 1000", " 1e999"),  # beyond the exact range
        DRAFT_BODY.replace(" 1000", " 9007199254740993"),  # above 2**53-1
    ],
)
def test_an_ambiguous_or_invalid_draft_group_is_unavailable(text: str):
    sample = parse_draft_document(text)
    assert sample.draft_tokens is None
    assert sample.draft_accepted is None


def test_one_broken_draft_member_nullifies_the_pair_not_just_itself():
    # A ratio needs both operands; a half pair would report a plausible lie.
    text = DRAFT_BODY.replace(" 1000", " 1e999")
    sample = parse_draft_document(text)
    assert (sample.draft_tokens, sample.draft_accepted) == (None, None)


# --------------------------------------------------------------------------- #
# Window fill timeline (AC2, AC3)
# --------------------------------------------------------------------------- #
def test_mtp_windows_fill_only_at_a_full_span_of_continuous_samples():
    engine, clock = LiveWindowEngine(), Clock()
    drafted = accepted = 0

    def tick(dt: float = 1.0) -> None:
        nonlocal drafted, accepted
        drafted, accepted = drafted + 10, accepted + 7
        engine.record_metrics(clock.advance(dt), WALL, draft_sample(drafted, accepted))

    engine.record_metrics(0.0, WALL, draft_sample(10, 7))  # the monitor's first poll
    for _ in range(4):
        tick()
    mtp = engine.snapshot(clock(), WALL)["mtp_acceptance"]
    assert mtp == {"window_5s": None, "window_1m": None, "window_30m": None, "lifetime": 0.7}

    tick()  # t = 5
    mtp = engine.snapshot(clock(), WALL)["mtp_acceptance"]
    assert mtp["window_5s"] == pytest.approx(0.7)
    assert mtp["window_1m"] is None and mtp["window_30m"] is None

    for _ in range(55):
        tick()
    assert clock() == 60
    mtp = engine.snapshot(clock(), WALL)["mtp_acceptance"]
    assert mtp["window_5s"] == pytest.approx(0.7)
    assert mtp["window_1m"] == pytest.approx(0.7)
    assert mtp["window_30m"] is None

    for _ in range(1739):
        tick()
    assert clock() == 1799
    assert engine.snapshot(clock(), WALL)["mtp_acceptance"]["window_30m"] is None

    tick()  # t = 1800
    assert engine.snapshot(clock(), WALL)["mtp_acceptance"]["window_30m"] == pytest.approx(0.7)


def test_moe_windows_fill_over_the_experts_pair_and_have_no_lifetime():
    engine, clock = LiveWindowEngine(), Clock()
    routed = res = 0

    def tick(dt: float = 1.0) -> None:
        nonlocal routed, res
        routed, res = routed + 100, res + 93
        engine.record_stats(
            clock.advance(dt), WALL, stats_sample(routed=routed, routed_res=res, uptime_s=100 + clock())
        )

    engine.record_stats(0.0, WALL, stats_sample(routed=0, routed_res=0, uptime_s=100))
    for _ in range(4):
        tick()
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"] == {
        "window_5s": None,
        "window_1m": None,
        "window_30m": None,
    }
    tick()  # t = 5
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] == pytest.approx(0.93)


def test_moe_window_uses_only_samples_carrying_the_experts_pair():
    engine, clock = LiveWindowEngine(), Clock()
    # A non-MoE model: the first 10 samples carry no experts block at all.
    for _ in range(10):
        engine.record_stats(clock.advance(1.0), WALL, stats_sample(routed=None, routed_res=None))
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] is None
    # The model is swapped for an MoE one: the pair appears at t=10 and advances.
    for step in range(1, 6):
        engine.record_stats(
            clock.advance(1.0), WALL, stats_sample(routed=100 * step, routed_res=70 * step)
        )
    # Five pair-bearing samples span 4 s: a 5 s window needs one more poll. Treating
    # the missing earlier pair as 0 would wrongly report 0.7 here.
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] is None
    engine.record_stats(clock.advance(1.0), WALL, stats_sample(routed=600, routed_res=420))
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] == pytest.approx(0.7)


def test_a_window_never_uses_a_baseline_older_than_the_span_plus_tolerance():
    engine, clock = LiveWindowEngine(), Clock()
    # Sparse-but-continuous sampling (2.9 s gaps, under the 3 s reset rule): the
    # only candidate baseline at t=7.3 is 7.3 s old, i.e. past 5 s + 2 s.
    drafted = accepted = 0
    for t in (0.0, 2.9, 5.8, 7.3):
        drafted, accepted = drafted + 29, accepted + 20
        engine.record_metrics(t, WALL, draft_sample(drafted, accepted))
    snapshot = engine.snapshot(7.3, WALL)
    assert snapshot["mtp_acceptance"]["window_5s"] is None
    assert snapshot["mtp_acceptance"]["lifetime"] == pytest.approx(20 / 29)


# --------------------------------------------------------------------------- #
# Idle denominators (AC4)
# --------------------------------------------------------------------------- #
def test_an_idle_window_is_a_placeholder_never_zero_percent():
    engine, clock = LiveWindowEngine(), Clock()
    engine.record_metrics(0.0, WALL, draft_sample(1000, 700))
    for _ in range(10):
        engine.record_metrics(clock.advance(1.0), WALL, draft_sample(1000, 700))
    mtp = engine.snapshot(clock(), WALL)["mtp_acceptance"]
    assert mtp["window_5s"] is None
    assert mtp["window_1m"] is None
    assert mtp["lifetime"] == pytest.approx(0.7)


def test_an_idle_moe_denominator_is_a_placeholder_too():
    engine, clock = LiveWindowEngine(), Clock()
    for _ in range(8):
        engine.record_stats(clock.advance(1.0), WALL, stats_sample(routed=500, routed_res=500))
    moe = engine.snapshot(clock(), WALL)["moe_gpu_hit"]
    assert moe["window_5s"] is None and moe["window_1m"] is None


def test_lifetime_is_null_until_anything_is_drafted():
    engine = LiveWindowEngine()
    engine.record_metrics(0.0, WALL, draft_sample(0, 0))
    assert engine.snapshot(0.0, WALL)["mtp_acceptance"]["lifetime"] is None


def test_lifetime_uses_the_latest_cumulative_totals():
    engine, clock = LiveWindowEngine(), Clock()
    engine.record_metrics(0.0, WALL, draft_sample(100, 60))
    engine.record_metrics(clock.advance(1.0), WALL, draft_sample(110, 67))
    assert engine.snapshot(clock(), WALL)["mtp_acceptance"]["lifetime"] == pytest.approx(67 / 110)


# --------------------------------------------------------------------------- #
# Run clearing (AC5)
# --------------------------------------------------------------------------- #
def feed_stats(engine: LiveWindowEngine, clock: Clock, count: int, cadence: float = 1.0) -> None:
    for _ in range(count):
        t = clock.advance(cadence)
        engine.record_stats(
            t,
            WALL,
            stats_sample(routed=int(t * 100), routed_res=int(t * 90), uptime_s=1000 + t),
        )


def test_uptime_decrease_clears_the_stats_run():
    engine, clock = LiveWindowEngine(), Clock()
    feed_stats(engine, clock, 7)
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] == pytest.approx(0.9)
    engine.record_stats(clock.advance(1.0), WALL, stats_sample(routed=50, routed_res=45, uptime_s=1.0))
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] is None
    # The gauges are current values, so they keep reporting across the restart.
    assert engine.snapshot(clock(), WALL)["requests"] == {"active": 3, "queued": 1}
    feed_stats(engine, clock, 4)
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] is None
    feed_stats(engine, clock, 1)
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] is not None


def test_a_live_counter_decrease_clears_the_stats_run():
    engine, clock = LiveWindowEngine(), Clock()
    feed_stats(engine, clock, 7)
    filled = engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"]
    assert filled == pytest.approx(0.9)
    # Same uptime, lower routed_res: an upstream counter reset, not a dip.
    engine.record_stats(
        clock.advance(1.0), WALL, stats_sample(routed=700, routed_res=1, uptime_s=1007.0)
    )
    assert engine.snapshot(clock(), WALL)["moe_gpu_hit"]["window_5s"] is None


def test_a_counter_decrease_clears_the_metrics_run():
    engine, clock = LiveWindowEngine(), Clock()
    for i in range(7):
        engine.record_metrics(float(i), WALL, draft_sample(10 * (i + 1), 7 * (i + 1)))
    assert engine.snapshot(6.0, WALL)["mtp_acceptance"]["window_5s"] == pytest.approx(0.7)
    engine.record_metrics(7.0, WALL, draft_sample(5, 3))
    assert engine.snapshot(7.0, WALL)["mtp_acceptance"]["window_5s"] is None
    assert engine.snapshot(7.0, WALL)["mtp_acceptance"]["lifetime"] == pytest.approx(0.6)


def test_a_gap_of_more_than_three_seconds_clears_the_run():
    engine, clock = LiveWindowEngine(), Clock()
    for i in range(6):
        engine.record_metrics(float(i), WALL, draft_sample(10 * (i + 1), 7 * (i + 1)))
    assert engine.snapshot(5.0, WALL)["mtp_acceptance"]["window_5s"] == pytest.approx(0.7)
    engine.record_metrics(9.0, WALL, draft_sample(70, 49))  # 4 s of silence
    assert engine.snapshot(9.0, WALL)["mtp_acceptance"]["window_5s"] is None
    for t in (10.0, 11.0, 12.0, 13.0):
        engine.record_metrics(t, WALL, draft_sample(70 + 10 * int(t - 9), 49 + 7 * int(t - 9)))
        assert engine.snapshot(t, WALL)["mtp_acceptance"]["window_5s"] is None
    engine.record_metrics(14.0, WALL, draft_sample(120, 84))
    assert engine.snapshot(14.0, WALL)["mtp_acceptance"]["window_5s"] == pytest.approx(0.7)


def test_a_three_second_gap_is_tolerated_by_the_run_rule():
    engine = LiveWindowEngine()
    engine.record_metrics(0.0, WALL, draft_sample(10, 7))
    engine.record_metrics(3.0, WALL, draft_sample(20, 14))
    engine.record_metrics(4.0, WALL, draft_sample(30, 21))
    engine.record_metrics(5.0, WALL, draft_sample(40, 28))
    engine.record_metrics(6.0, WALL, draft_sample(50, 35))
    engine.record_metrics(7.0, WALL, draft_sample(60, 42))
    assert engine.snapshot(7.0, WALL)["mtp_acceptance"]["window_5s"] == pytest.approx(0.7)


# --------------------------------------------------------------------------- #
# Freshness and state (AC6, plan D5)
# --------------------------------------------------------------------------- #
def test_a_source_silent_for_more_than_three_seconds_goes_null():
    engine = LiveWindowEngine()
    engine.record_stats(0.0, WALL, stats_sample())
    engine.record_metrics(0.0, WALL, draft_sample(100, 70))

    fresh = engine.snapshot(3.0, WALL)
    assert fresh["state"] == "live"
    assert fresh["requests"] == {"active": 3, "queued": 1}
    assert fresh["mtp_acceptance"]["lifetime"] == pytest.approx(0.7)

    stale = engine.snapshot(3.001, WALL)
    assert stale["state"] == "stale"
    assert stale["observed_at"] is None
    assert stale["requests"] == {"active": None, "queued": None}
    assert stale["prefill_tokens_per_second"] is None
    assert stale["decode_tokens_per_second"] is None
    assert stale["decode_tokens_per_second_per_request"] is None
    assert stale["mtp_acceptance"] == {
        "window_5s": None,
        "window_1m": None,
        "window_30m": None,
        "lifetime": None,
    }
    assert stale["moe_gpu_hit"] == {"window_5s": None, "window_1m": None, "window_30m": None}


def test_one_fresh_source_keeps_the_snapshot_live_and_the_other_null():
    engine, clock = LiveWindowEngine(), Clock()
    engine.record_stats(0.0, WALL, stats_sample())
    engine.record_metrics(0.0, WALL, draft_sample(100, 70))
    for _ in range(5):
        engine.record_stats(clock.advance(1.0), WALL, stats_sample())
    snapshot = engine.snapshot(clock(), WALL)
    assert snapshot["state"] == "live"
    assert snapshot["requests"] == {"active": 3, "queued": 1}
    assert snapshot["mtp_acceptance"]["lifetime"] is None
    assert snapshot["observed_at"] is not None


def test_the_snapshot_starts_and_returns_to_live():
    engine, clock = LiveWindowEngine(), Clock()
    starting = engine.snapshot(clock(), WALL)
    assert starting["state"] == "starting"
    assert starting["observed_at"] is None
    engine.record_stats(clock.advance(1.0), WALL, stats_sample())
    assert engine.snapshot(clock(), WALL)["state"] == "live"
    clock.advance(10.0)
    assert engine.snapshot(clock(), WALL)["state"] == "stale"
    engine.record_stats(clock.advance(1.0), WALL, stats_sample())
    assert engine.snapshot(clock(), WALL)["state"] == "live"


# --------------------------------------------------------------------------- #
# Current-only tiles (AC1) and the wire shape (plan D5)
# --------------------------------------------------------------------------- #
def test_decode_per_request_divides_by_active_requests_only():
    engine = LiveWindowEngine()
    engine.record_stats(0.0, WALL, stats_sample(running=3, decode_tps=180.2))
    assert engine.snapshot(0.0, WALL)["decode_tokens_per_second_per_request"] == pytest.approx(60.0666667)
    engine.record_stats(1.0, WALL, stats_sample(running=0, decode_tps=180.2))
    snapshot = engine.snapshot(1.0, WALL)
    assert snapshot["decode_tokens_per_second_per_request"] is None
    assert snapshot["decode_tokens_per_second"] == pytest.approx(180.2)
    engine.record_stats(2.0, WALL, stats_sample(running=None, decode_tps=180.2))
    assert engine.snapshot(2.0, WALL)["decode_tokens_per_second_per_request"] is None


def test_the_snapshot_carries_the_route_contract_exactly():
    engine = LiveWindowEngine()
    engine.record_stats(0.0, WALL, stats_sample())
    engine.record_metrics(0.0, WALL, draft_sample(1000, 700))
    snapshot = engine.snapshot(0.0, WALL)
    assert set(snapshot) == {
        "state",
        "observed_at",
        "requests",
        "prefill_tokens_per_second",
        "decode_tokens_per_second",
        "decode_tokens_per_second_per_request",
        "mtp_acceptance",
        "moe_gpu_hit",
    }
    assert snapshot["observed_at"] == "2026-10-27T12:00:00.000000Z"
    assert set(snapshot["mtp_acceptance"]) == {"window_5s", "window_1m", "window_30m", "lifetime"}
    assert set(snapshot["moe_gpu_hit"]) == {"window_5s", "window_1m", "window_30m"}


def test_the_snapshot_is_a_plain_json_document():
    engine = LiveWindowEngine()
    engine.record_stats(0.0, WALL, stats_sample())
    engine.record_metrics(0.0, WALL, draft_sample(1000, 700))
    json.dumps(engine.snapshot(0.0, WALL))


def test_the_engine_reports_source_freshness_for_transition_logging():
    engine, clock = LiveWindowEngine(), Clock()
    assert engine.source_fresh("stats", 0.0) is False
    engine.record_stats(0.0, WALL, stats_sample())
    assert engine.source_fresh("stats", 0.0) is True
    assert engine.source_fresh("metrics", 0.0) is False
    assert engine.moe_available(0.0) is True
    assert engine.mtp_available(0.0) is False
    engine.record_stats(clock.advance(4.0), WALL, stats_sample(routed=None, routed_res=None))
    assert engine.moe_available(clock()) is False


def test_the_live_clock_is_never_read_inside_the_engine():
    # The engine takes its clock as an argument on every call; nothing is cached.
    engine = LiveWindowEngine()
    engine.record_stats(0.0, WALL, stats_sample())
    assert engine.snapshot(0.0, WALL)["state"] == "live"
    assert engine.snapshot(1000.0, WALL)["state"] == "stale"


# --------------------------------------------------------------------------- #
# LiveMonitor: cadence, lifecycle, transition-only logging (plan Step 2)
# --------------------------------------------------------------------------- #
class StubFetcher:
    """Stands in for UpstreamFetcher: scripted bodies, no sockets."""

    def __init__(self, stats: object = STATS_BODY, metrics: object = b"") -> None:
        self.bodies: dict[str, object] = {"/stats": stats, "/metrics": metrics}
        self.calls: list[str] = []
        self.clock: Clock | None = None
        self.stats_seconds = 0.0

    def fetch(self, path: str) -> bytes:
        self.calls.append(path)
        if path == "/stats" and self.clock is not None and self.stats_seconds:
            self.clock.advance(self.stats_seconds)
        body = self.bodies[path]
        if isinstance(body, Exception):
            raise body
        return body  # type: ignore[return-value]


def monitor_config(**overrides: object) -> Config:
    values: dict[str, object] = {
        "radiance_base_url": "http://127.0.0.1:8000",
        "source_id": "s1",
        "poll_interval_seconds": 15,
        "http_timeout_seconds": 3.0,
        "database_path": "/tmp/live-monitor.sqlite3",
        "raw_retention_days": 30,
        "port": 8080,
        "log_level": "INFO",
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


def make_monitor(fetcher: StubFetcher, clock: Clock, **config_kw: object) -> LiveMonitor:
    def sleeper(delay: float) -> None:
        clock.advance(delay)

    return LiveMonitor(
        monitor_config(**config_kw),
        fetcher=fetcher,
        monotonic=clock,
        wall_clock=lambda: WALL,
        sleeper=sleeper,
    )


def test_the_poller_keeps_a_fixed_grid_and_skips_missed_slots():
    """A 2.4 s fetch must skip slots, never queue a burst of catch-up polls."""
    clock, sleeps = Clock(), []
    fetcher = StubFetcher()
    fetcher.clock, fetcher.stats_seconds = clock, 2.4
    monitor = make_monitor(fetcher, clock)

    def sleeper(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)
        if len(sleeps) >= 4:
            monitor.request_stop()

    monitor._sleep = sleeper
    monitor._run()
    # Slot boundaries stay on the 1 s grid: every cycle resumes at the next
    # whole second after the overruns, so the sleep is a constant 0.6 s.
    assert sleeps == pytest.approx([0.6, 0.6, 0.6, 0.6])
    assert clock.t == pytest.approx(12.0)
    assert fetcher.calls.count("/stats") == 4


def test_a_fast_cycle_waits_the_full_second_between_polls():
    clock, sleeps = Clock(), []
    fetcher = StubFetcher()
    monitor = make_monitor(fetcher, clock)

    def sleeper(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)
        if len(sleeps) >= 3:
            monitor.request_stop()

    monitor._sleep = sleeper
    monitor._run()
    assert sleeps == [1.0, 1.0, 1.0]
    assert fetcher.calls == ["/stats", "/metrics"] * 3


def test_the_stop_request_ends_the_thread_inside_the_join_budget():
    monitor = LiveMonitor(
        monitor_config(), fetcher=StubFetcher(), wall_clock=lambda: WALL
    )
    monitor.start()
    monitor.start()  # idempotent: exactly one poller thread
    assert sum(t.name == "jarvis-live" for t in threading.enumerate()) == 1
    began = time.monotonic()
    monitor.request_stop()
    assert monitor.join(1.0) is True
    assert time.monotonic() - began < 1.0
    assert monitor.stopped() is True


def test_a_published_snapshot_carries_the_live_values():
    clock = Clock()
    monitor = make_monitor(StubFetcher(stats=STATS_BODY, metrics=DRAFT_BODY.encode()), clock)
    monitor.poll_once()
    snapshot = monitor.snapshot()
    assert snapshot["state"] == "live"
    assert snapshot["requests"] == {"active": 3, "queued": 1}
    assert snapshot["mtp_acceptance"]["lifetime"] == pytest.approx(0.7)
    assert snapshot["observed_at"].endswith("Z")


def test_a_failing_stats_source_nullifies_only_the_stats_tiles():
    clock = Clock()
    fetcher = StubFetcher(stats=UpstreamError("upstream_unavailable"), metrics=DRAFT_BODY.encode())
    monitor = make_monitor(fetcher, clock)
    monitor.poll_once()
    snapshot = monitor.snapshot()
    assert snapshot["state"] == "live"
    assert snapshot["requests"]["active"] is None
    assert snapshot["decode_tokens_per_second"] is None
    assert snapshot["mtp_acceptance"]["lifetime"] == pytest.approx(0.7)


def test_logging_happens_only_on_transitions_never_per_poll(caplog):
    clock = Clock()
    fetcher = StubFetcher(stats=STATS_BODY, metrics=DRAFT_BODY.encode())
    monitor = make_monitor(fetcher, clock)
    with caplog.at_level(logging.INFO, logger="jarvis_dashboard.live"):
        monitor.poll_once()
        monitor.poll_once()  # steady state: nothing new to say
        fetcher.bodies["/stats"] = UpstreamError("upstream_unavailable")
        fetcher.bodies["/metrics"] = UpstreamError("upstream_unavailable")
        for _ in range(5):  # five consecutive failed polls
            clock.advance(4.0)
            monitor.poll_once()

    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("live state starting -> live") == 1
    assert messages.count("live state live -> stale") == 1
    assert messages.count("live source stats recovered") == 1
    assert messages.count("live source stats went stale") == 1
    assert messages.count("live source metrics recovered") == 1
    assert messages.count("live source metrics went stale") == 1
    assert messages.count("live field experts present") == 1
    assert messages.count("live field experts absent") == 1
    assert messages.count("live field draft present") == 1
    assert messages.count("live field draft absent") == 1
    # 2 state lines + 4 source lines + 4 field lines: a failing poller adds
    # nothing per poll, so a 1 Hz outage cannot flood the log.
    assert len(messages) == 10


def test_a_run_reset_is_logged_once_per_event(caplog):
    clock = Clock()
    restart = json.dumps(
        {**json.loads(STATS_BODY), "uptime_s": 1.0, "experts": {"routed": 5, "routed_res": 4}}
    ).encode()
    fetcher = StubFetcher(stats=STATS_BODY, metrics=DRAFT_BODY.encode())
    monitor = make_monitor(fetcher, clock)
    with caplog.at_level(logging.INFO, logger="jarvis_dashboard.live"):
        monitor.poll_once()
        fetcher.bodies["/stats"] = restart  # uptime drop = a new run
        clock.advance(1.0)
        monitor.poll_once()
        fetcher.bodies["/stats"] = json.dumps(
            {**json.loads(restart), "uptime_s": 2.0, "experts": {"routed": 6, "routed_res": 5}}
        ).encode()
        clock.advance(1.0)
        monitor.poll_once()

    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("live stats run reset") == 1


def test_the_live_fetch_timeout_stays_below_the_slot():
    assert LiveMonitor(monitor_config()).fetch_timeout_seconds == 0.9
    assert (
        LiveMonitor(monitor_config(http_timeout_seconds=0.4)).fetch_timeout_seconds == 0.4
    )


def test_a_metrics_body_that_is_not_utf8_nullifies_the_mtp_group_only():
    # Plan D3: one unusable source may only cost its own tiles, never the row.
    clock = Clock()
    monitor = make_monitor(StubFetcher(stats=STATS_BODY, metrics=b"\xff\xfe\x00binary"), clock)
    monitor.poll_once()

    snapshot = monitor.snapshot()
    assert snapshot["state"] == "live"
    assert snapshot["requests"] == {"active": 3, "queued": 1}
    assert snapshot["prefill_tokens_per_second"] == 1240.4
    assert snapshot["mtp_acceptance"] == {
        "window_5s": None,
        "window_1m": None,
        "window_30m": None,
        "lifetime": None,
    }


def test_a_poll_blocked_past_the_join_budget_is_reported(caplog):
    # Plan D1: the poller must never be the reason shutdown hangs, so the overrun
    # has to be visible in the log rather than silent.
    entered = threading.Event()
    release = threading.Event()

    class StuckFetcher(StubFetcher):
        def fetch(self, path: str) -> bytes:
            entered.set()
            release.wait(5.0)
            return b""

    monitor = LiveMonitor(
        monitor_config(), fetcher=StuckFetcher(), wall_clock=lambda: WALL
    )
    with caplog.at_level(logging.INFO, logger="jarvis_dashboard.live"):
        monitor.start()
        assert entered.wait(2.0), "the poller never reached its fetch"
        assert monitor.join(0.1) is False
        assert "live poller exceeded the shutdown budget" in caplog.text
        monitor.request_stop()
        release.set()
        assert monitor.join(5.0) is True
