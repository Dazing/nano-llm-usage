from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jarvis_dashboard.accounting import transition_series
from jarvis_dashboard.domain import (
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    EpochReason,
    QualityFlag,
    ScrapeCounters,
    StatsEvidence,
    checked_add_int64,
)

T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
MODEL = "fixture-model"
E0 = {"model_name": MODEL, "engine": "0"}
E1 = {"model_name": MODEL, "engine": "1"}


def counters(labels, ci=None, go=None, ri=None, ro=None):
    values = {COMPUTED_INPUT: ci, GENERATED_OUTPUT: go, "request_input": ri, "request_output": ro}
    problems = {k: "missing" for k, v in values.items() if v is None}
    return ScrapeCounters(labels=dict(labels), values=values, problems=problems)


def stats(at: datetime, uptime: float, dur: float = 0.0, model: str = MODEL):
    return StatsEvidence(model=model, uptime_s=uptime, request_start=at, response_end=at + timedelta(seconds=dur))


def poll(state, t, ci, go, ri=None, ro=None, st=None, labels=E0, interval=15):
    return transition_series(state, counters(labels, ci, go, ri, ro), t, st, interval)


def deltas(plan, key):
    return sum(hd.tokens for hd in plan.hour_deltas if hd.counter_key == key)


# ---------------------------------------------------------------- baseline


def test_first_observation_opens_baseline_without_credit():
    # Scenario A1
    plan = poll(None, T0, 1000, 100, st=stats(T0, 100))
    assert plan.is_new
    assert plan.new_epoch is not None and plan.new_epoch.reason is EpochReason.NEW_SERIES
    assert plan.new_epoch.opening_computed_input == 1000
    assert plan.new_epoch.opening_generated_output == 100
    assert plan.sample is None
    assert plan.hour_deltas == []


def test_increasing_counters_credit_deltas():
    # Scenario A2: 900 computed / 60 generated / 960 total over two polls
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    s = p1.state
    p2 = poll(s, T0 + timedelta(seconds=15), 1600, 140, st=stats(T0 + timedelta(seconds=15), 115))
    p3 = poll(p2.state, T0 + timedelta(seconds=30), 1900, 160, st=stats(T0 + timedelta(seconds=30), 130))
    ci = deltas(p2, COMPUTED_INPUT) + deltas(p3, COMPUTED_INPUT)
    go = deltas(p2, GENERATED_OUTPUT) + deltas(p3, GENERATED_OUTPUT)
    assert (ci, go, ci + go) == (900, 60, 960)
    assert p3.sample is not None and p3.sample.rate_eligible
    assert p3.sample.quality == frozenset()


def test_unchanged_counters_credit_observed_zero_not_null():
    # Scenario A3 / D26
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    p2 = poll(p1.state, T0 + timedelta(seconds=15), 1000, 100, st=stats(T0 + timedelta(seconds=15), 115))
    assert p2.sample is not None
    assert p2.sample.deltas[COMPUTED_INPUT] == 0
    assert p2.sample.deltas[GENERATED_OUTPUT] == 0
    assert deltas(p2, COMPUTED_INPUT) == 0


def test_missing_primary_pair_credits_null_not_zero():
    # Scenario C19
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    broken = counters(E0, ci=None, go=140)
    p2 = transition_series(p1.state, broken, T0 + timedelta(seconds=15), stats(T0 + timedelta(seconds=15), 115), 15)
    assert p2.sample is not None
    assert p2.sample.deltas[COMPUTED_INPUT] is None
    assert QualityFlag.COUNTER_UNAVAILABLE in p2.sample.quality
    # baseline retained for the next usable poll
    p3 = poll(p2.state, T0 + timedelta(seconds=30), 1100, 150, st=stats(T0 + timedelta(seconds=30), 130))
    assert deltas(p3, COMPUTED_INPUT) == 100


# ------------------------------------------------------- engines/optionals


def test_engine1_series_never_borrows_uptime():
    # Scenario A4: engine 1 deltas accepted but continuity unverified
    p1 = poll(None, T0, 500, 50, st=stats(T0, 100), labels=E1)
    p2 = poll(p1.state, T0 + timedelta(seconds=15), 520, 55, st=stats(T0 + timedelta(seconds=15), 115), labels=E1)
    assert deltas(p2, COMPUTED_INPUT) == 20
    assert QualityFlag.CONTINUITY_UNVERIFIED in p2.sample.quality
    assert not p2.sample.rate_eligible


def test_optional_counters_tracked_independently():
    # Scenario A5: request counters produce their own deltas
    p1 = poll(None, T0, 1000, 100, ri=10, ro=5, st=stats(T0, 100))
    p2 = poll(p1.state, T0 + timedelta(seconds=15), 1100, 110, ri=14, ro=7, st=stats(T0 + timedelta(seconds=15), 115))
    assert deltas(p2, "request_input") == 4
    assert deltas(p2, "request_output") == 2
    assert deltas(p2, COMPUTED_INPUT) == 100


def test_optional_reset_does_not_touch_primary():
    # Scenario A6: optional decrease resets only its own baseline
    p1 = poll(None, T0, 1000, 100, ro=50, st=stats(T0, 100))
    p2 = poll(p1.state, T0 + timedelta(seconds=15), 1100, 110, ro=2, st=stats(T0 + timedelta(seconds=15), 115))
    assert deltas(p2, COMPUTED_INPUT) == 100  # primary unaffected
    assert deltas(p2, "request_output") == 0
    assert any(e.kind == "optional_reset" for e in p2.events)
    p3 = poll(p2.state, T0 + timedelta(seconds=30), 1200, 120, ro=9, st=stats(T0 + timedelta(seconds=30), 130))
    assert deltas(p3, "request_output") == 7
    assert deltas(p3, COMPUTED_INPUT) == 100


# ---------------------------------------------------------------- resets


def test_primary_reset_credits_from_new_baseline():
    # Scenario B7: (1900,160) -> (50,5) -> (90,8) yields +40/+3
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    p2 = poll(p1.state, T0 + timedelta(seconds=15), 1900, 160, st=stats(T0 + timedelta(seconds=15), 115))
    p3 = poll(p2.state, T0 + timedelta(seconds=30), 50, 5, st=stats(T0 + timedelta(seconds=30), 130))
    assert p3.new_epoch is not None and p3.new_epoch.reason is EpochReason.COUNTER_RESET
    assert p3.sample is not None
    assert p3.sample.deltas[COMPUTED_INPUT] is None  # reset poll credits nothing
    assert deltas(p3, COMPUTED_INPUT) == 0
    p4 = poll(p3.state, T0 + timedelta(seconds=45), 90, 8, st=stats(T0 + timedelta(seconds=45), 145))
    assert deltas(p4, COMPUTED_INPUT) == 40
    assert deltas(p4, GENERATED_OUTPUT) == 3
    all_ci = deltas(p2, COMPUTED_INPUT) + deltas(p4, COMPUTED_INPUT)
    all_go = deltas(p2, GENERATED_OUTPUT) + deltas(p4, GENERATED_OUTPUT)
    assert all_ci == 940
    assert all_ci + all_go == 1003  # Scenario B7 lifetime total


def test_decrease_rebaselines_both_primary_counters():
    # Scenario B8: one primary decrease rebaselines the coupled pair
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    p2 = poll(p1.state, T0 + timedelta(seconds=15), 1900, 160, st=stats(T0 + timedelta(seconds=15), 115))
    p3 = poll(p2.state, T0 + timedelta(seconds=30), 1899, 159, st=stats(T0 + timedelta(seconds=30), 130))
    assert p3.new_epoch is not None and p3.new_epoch.reason is EpochReason.COUNTER_RESET
    assert p3.state.primary.computed_input == 1899
    assert p3.state.primary.generated_output == 159
    p4 = poll(p3.state, T0 + timedelta(seconds=45), 1900, 161, st=stats(T0 + timedelta(seconds=45), 145))
    assert deltas(p4, COMPUTED_INPUT) == 1
    assert deltas(p4, GENERATED_OUTPUT) == 2


def test_restart_hidden_by_increasing_uptime_gets_new_epoch():
    # Scenario B9: disjoint process-start intervals -> restart evidence, no credit
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    t2 = T0 + timedelta(seconds=300)
    p2 = poll(p1.state, t2, 1500, 150, st=stats(t2, 200))  # uptime grew, intervals disjoint
    assert p2.new_epoch is not None and p2.new_epoch.reason is EpochReason.RESTART_EVIDENCE
    assert p2.sample is not None
    assert p2.sample.deltas[COMPUTED_INPUT] is None
    assert deltas(p2, COMPUTED_INPUT) == 0
    assert QualityFlag.RESET_BOUNDARY in p2.sample.quality
    # next poll continues from the new baseline
    t3 = t2 + timedelta(seconds=15)
    p3 = poll(p2.state, t3, 1600, 170, st=stats(t3, 215))
    assert deltas(p3, COMPUTED_INPUT) == 100


def test_missing_stats_accepts_deltas_unverified():
    # Scenario B10
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    p2 = poll(p1.state, T0 + timedelta(seconds=15), 1200, 120, st=None)
    assert deltas(p2, COMPUTED_INPUT) == 200
    assert QualityFlag.CONTINUITY_UNVERIFIED in p2.sample.quality
    assert not p2.sample.rate_eligible
    # stats recovery re-establishes the chain
    p3 = poll(p2.state, T0 + timedelta(seconds=30), 1300, 130, st=stats(T0 + timedelta(seconds=30), 130))
    assert deltas(p3, COMPUTED_INPUT) == 100


def test_clock_regression_new_epoch_no_credit():
    # Scenario B16
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    p2 = poll(p1.state, T0 - timedelta(seconds=15), 1010, 101, st=stats(T0, 100))
    assert p2.new_epoch is not None and p2.new_epoch.reason is EpochReason.CLOCK_REGRESSION
    assert p2.sample is not None and p2.sample.deltas[COMPUTED_INPUT] is None
    assert QualityFlag.CLOCK_DISCONTINUITY in p2.sample.quality
    assert p2.hour_deltas == []
    p3 = poll(p2.state, T0 + timedelta(seconds=15), 1050, 105, st=stats(T0 + timedelta(seconds=15), 115))
    assert deltas(p3, COMPUTED_INPUT) == 40  # from the rebaselined 1010


def test_endpoint_change_hint_rebaselines():
    # Scenario D30 (accounting part): same source, new endpoint -> fresh baseline
    p1 = poll(None, T0, 1000, 100, st=stats(T0, 100))
    p2 = transition_series(
        p1.state,
        counters(E0, 2000, 200),
        T0 + timedelta(seconds=15),
        None,
        15,
        new_epoch_hint=EpochReason.ENDPOINT_CHANGE,
    )
    assert p2.new_epoch is not None and p2.new_epoch.reason is EpochReason.ENDPOINT_CHANGE
    assert p2.sample is not None and p2.sample.deltas[COMPUTED_INPUT] is None
    assert p2.hour_deltas == []
    assert p2.new_epoch.opening_computed_input == 2000


# ------------------------------------------------------------ attribution


def test_hour_crossing_credits_ending_hour_only():
    # Scenario C17: 10:59:50 -> 11:00:05
    tb = datetime(2026, 1, 15, 10, 59, 50, tzinfo=UTC)
    te = datetime(2026, 1, 15, 11, 0, 5, tzinfo=UTC)
    p1 = poll(None, tb, 1000, 100, st=stats(tb, 100))
    p2 = poll(p1.state, te, 1500, 300, st=stats(te, 115))
    assert {hd.hour for hd in p2.hour_deltas} == {"2026-01-15T11Z"}
    assert deltas(p2, COMPUTED_INPUT) == 500
    assert QualityFlag.TIME_ATTRIBUTION_APPROXIMATE in p2.sample.quality


def test_int64_overflow_guard():
    # Scenario C22 (aggregate side)
    assert checked_add_int64(2**63 - 1, 0) == 2**63 - 1
    with pytest.raises(OverflowError):
        checked_add_int64(2**63 - 1, 1)
    with pytest.raises(OverflowError):
        checked_add_int64(-5, -1)
