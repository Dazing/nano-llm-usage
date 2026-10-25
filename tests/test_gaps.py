from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jarvis_dashboard.accounting import transition_series
from jarvis_dashboard.domain import (
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    PRIMARY_SCOPE,
    QualityFlag,
    ScrapeCounters,
    StatsEvidence,
)

T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
MODEL = "fixture-model"
E0 = {"model_name": MODEL, "engine": "0"}


def counters(ci=None, go=None, ri=None, ro=None):
    values = {COMPUTED_INPUT: ci, GENERATED_OUTPUT: go, "request_input": ri, "request_output": ro}
    problems = {k: "missing" for k, v in values.items() if v is None}
    return ScrapeCounters(labels=dict(E0), values=values, problems=problems)


def stats(at: datetime, uptime: float):
    return StatsEvidence(model=MODEL, uptime_s=uptime, request_start=at, response_end=at)


def deltas(plan, key):
    return sum(hd.tokens for hd in plan.hour_deltas if hd.counter_key == key)


def test_gap_recovery_credits_continuity_verified_deltas_once():
    # Scenario B11: (100,10)@10:00:00 -> (400,40)@10:03:00 compatible evidence
    p1 = transition_series(None, counters(100, 10), T0, stats(T0, 100), 15)
    t2 = T0 + timedelta(seconds=180)
    p2 = transition_series(p1.state, counters(400, 40), t2, stats(t2, 280), 15)
    assert deltas(p2, COMPUTED_INPUT) == 300
    assert deltas(p2, GENERATED_OUTPUT) == 30
    assert QualityFlag.COLLECTION_GAP in p2.sample.quality
    assert QualityFlag.TIME_ATTRIBUTION_APPROXIMATE in p2.sample.quality
    assert not p2.sample.rate_eligible
    assert len(p2.gaps_closed) == 1
    span = p2.gaps_closed[0]
    assert span.scope == PRIMARY_SCOPE and span.started_at == T0 and span.ended_at == t2
    # the gap is recorded once, not re-opened afterwards
    t3 = t2 + timedelta(seconds=15)
    p3 = transition_series(p2.state, counters(410, 42), t3, stats(t3, 295), 15)
    assert deltas(p3, COMPUTED_INPUT) == 10
    assert p3.gaps_closed == []
    assert QualityFlag.COLLECTION_GAP not in p3.sample.quality


def test_gap_recovery_with_disjoint_evidence_credits_nothing():
    # Scenario B12: uptime evidence proves a restart across the gap
    p1 = transition_series(None, counters(100, 10), T0, stats(T0, 100), 15)
    t2 = T0 + timedelta(seconds=180)
    p2 = transition_series(p1.state, counters(400, 40), t2, stats(t2, 30), 15)  # process started in the gap
    assert deltas(p2, COMPUTED_INPUT) == 0
    assert p2.sample.deltas[COMPUTED_INPUT] is None


def test_boundary_at_exactly_two_intervals_is_not_a_gap():
    # Scenario B14: gap only when elapsed > 2 * poll strictly
    p1 = transition_series(None, counters(100, 10), T0, stats(T0, 100), 15)
    t2 = T0 + timedelta(seconds=30)
    p2 = transition_series(p1.state, counters(130, 13), t2, stats(t2, 130), 15)
    assert QualityFlag.COLLECTION_GAP not in p2.sample.quality
    assert deltas(p2, COMPUTED_INPUT) == 30
    t3 = t2 + timedelta(seconds=31)
    p3 = transition_series(p2.state, counters(160, 16), t3, stats(t3, 161), 15)
    assert QualityFlag.COLLECTION_GAP in p3.sample.quality
    assert deltas(p3, COMPUTED_INPUT) == 30  # still credited, with gap flags


def test_series_absent_opens_gaps_and_keeps_baseline():
    # Scenario B13
    p1 = transition_series(None, counters(1000, 100), T0, stats(T0, 100), 15)
    t2 = T0 + timedelta(seconds=15)
    p2 = transition_series(p1.state, None, t2, stats(t2, 115), 15)
    assert p2.sample is None
    assert p2.hour_deltas == []
    assert PRIMARY_SCOPE in p2.state.open_gaps
    assert p2.state.open_gaps[PRIMARY_SCOPE].started_at == T0
    assert p2.state.stale_since == t2
    # baseline retained: reappearance continues
    t3 = T0 + timedelta(seconds=60)
    p3 = transition_series(p2.state, counters(1100, 110), t3, stats(t3, 160), 15)
    assert deltas(p3, COMPUTED_INPUT) == 100
    assert PRIMARY_SCOPE not in p3.state.open_gaps
    assert p3.state.stale_since is None
    assert QualityFlag.COLLECTION_GAP in p3.sample.quality


def test_gap_span_splits_across_hours():
    # Scenario C18: 23:30 -> 00:30 midnight gap splits into two hourly buckets
    start = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
    end = datetime(2026, 1, 16, 0, 30, tzinfo=UTC)
    p1 = transition_series(None, counters(100, 10), start, stats(start, 100), 15)
    p2 = transition_series(p1.state, counters(200, 20), end, stats(end, 100 + 3600), 15)
    by_hour = {hg.hour: hg.seconds for hg in p2.hour_gaps if hg.scope == PRIMARY_SCOPE}
    assert by_hour == {"2026-01-15T23Z": 1800.0, "2026-01-16T00Z": 1800.0}
    assert deltas(p2, COMPUTED_INPUT) == 100  # credited once, at the end hour
    assert {hd.hour for hd in p2.hour_deltas if hd.counter_key == COMPUTED_INPUT} == {"2026-01-16T00Z"}


def test_collector_downtime_gap_flags():
    # Scenario B14 (collector stopped): long outage credited once with gap flags
    p1 = transition_series(None, counters(100, 10), T0, stats(T0, 100), 15)
    t2 = T0 + timedelta(minutes=10)
    p2 = transition_series(p1.state, counters(700, 70), t2, stats(t2, 700), 15)
    assert deltas(p2, COMPUTED_INPUT) == 600
    assert QualityFlag.COLLECTION_GAP in p2.sample.quality
    assert not p2.sample.rate_eligible
