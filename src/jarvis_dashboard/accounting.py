"""Deterministic counter transitions (specification D4, delivery plan D1/D3/D4).

``transition_series`` is pure: it receives committed per-series state, the
validated scrape counters for one identity, an explicit observation UTC time,
optional /stats evidence and the poll interval, and returns the proposed next
state plus deltas, hourly contributions, gap changes, epoch changes and
quality flags. No HTTP, clock reads or SQL happen here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from .domain import (
    COUNTER_KEYS,
    GENERATED_OUTPUT,
    COMPUTED_INPUT,
    OPTIONAL_KEYS,
    PRIMARY_KEYS,
    PRIMARY_SCOPE,
    QualityFlag,
    EpochReason,
    OpenGap,
    OptionalSlot,
    PrimarySlot,
    ScrapeCounters,
    SeriesState,
    StatsEvidence,
    hour_floor,
    hour_label,
    split_span_by_hour,
)

# An elapsed interval strictly greater than this many poll intervals is a gap
# even without an explicit failed scrape (specification D4 item 6).
GAP_INTERVAL_MULTIPLIER = 2


@dataclass(frozen=True)
class HourDelta:
    counter_key: str
    hour: str  # canonical hour label
    tokens: int
    observed_intervals: int
    # Delivery 2 coverage metadata: the contribution's own ending observation,
    # interval length, rate eligibility and quality flags. The hourly row keeps
    # the union of contributing qualities plus first/last bounds so coverage
    # survives raw-sample retention (Delivery 2 plan Step 2).
    observed_at: datetime | None = None
    interval_seconds: float | None = None
    eligible: bool = False
    quality: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HourGap:
    scope: str
    hour: str
    seconds: float


@dataclass(frozen=True)
class ClosedSpan:
    scope: str
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True)
class NewEpoch:
    reason: EpochReason
    started_at: datetime
    opening_computed_input: int | None
    opening_generated_output: int | None


@dataclass(frozen=True)
class SamplePlan:
    observed_at: datetime
    counters: dict[str, int | None]
    deltas: dict[str, int | None]
    quality: frozenset[str]
    rate_eligible: bool


@dataclass(frozen=True)
class EventPlan:
    kind: str
    occurred_at: datetime
    details: dict[str, str | int | float] = field(default_factory=dict)


@dataclass(frozen=True)
class SeriesPlan:
    labels: dict[str, str]
    is_new: bool
    new_epoch: NewEpoch | None
    sample: SamplePlan | None
    state: SeriesState
    hour_deltas: list[HourDelta]
    hour_gaps: list[HourGap]
    gaps_closed: list[ClosedSpan]
    gaps_opened: list[OpenGap]
    events: list[EventPlan]


def _evidence_applies(labels: dict[str, str], stats: StatsEvidence | None) -> bool:
    """Match stats only when model equals the series model_name and engine is 0."""
    if stats is None:
        return False
    return labels.get("engine") == "0" and labels.get("model_name") == stats.model


def _evidence_of(stats: StatsEvidence) -> tuple[datetime, datetime]:
    return stats.process_start_interval()


def _state_interval(state: SeriesState) -> tuple[datetime, datetime] | None:
    if state.primary.uptime_lo is None or state.primary.uptime_hi is None:
        return None
    return (state.primary.uptime_lo, state.primary.uptime_hi)


def _close_scope(
    plan_gaps: dict[str, OpenGap],
    closed: list[ClosedSpan],
    hour_gaps: list[HourGap],
    scope: str,
    ended_at: datetime,
) -> bool:
    """Close an open gap span; return True when one was open."""
    gap = plan_gaps.pop(scope, None)
    if gap is None:
        return False
    closed.append(ClosedSpan(scope, gap.started_at, ended_at))
    for hour, seconds, _start, _end in split_span_by_hour(gap.started_at, ended_at):
        hour_gaps.append(HourGap(scope, hour_label(hour), seconds))
    return True


def _open_scope(
    plan_gaps: dict[str, OpenGap],
    scope: str,
    last_usable: datetime | None,
) -> OpenGap | None:
    """Open a gap from the last usable observation; no fabricated pre-history."""
    if scope in plan_gaps or last_usable is None:
        return None
    gap = OpenGap(scope, last_usable)
    plan_gaps[scope] = gap
    return gap


def _elapsed_is_gap(elapsed: timedelta | None, poll_interval_s: int) -> bool:
    if elapsed is None:
        return False
    return elapsed > timedelta(seconds=GAP_INTERVAL_MULTIPLIER * poll_interval_s)


def _optional_transition(
    key: str,
    slot: OptionalSlot,
    value: int | None,
    observed_at: datetime,
    gaps: dict[str, OpenGap],
    closed: list[ClosedSpan],
    hour_gaps: list[HourGap],
    events: list[EventPlan],
    hour_deltas: list[HourDelta],
    poll_interval_s: int,
    verified_now: bool,
    source_restart: bool,
) -> tuple[OptionalSlot, set[str]]:
    """Independent baseline/continuity for one optional counter."""
    scope = key
    if value is None:
        _open_scope(gaps, scope, slot.observed_at)
        # Optional counters are commonly absent; that is not a sample defect.
        return slot, set()
    if slot.baseline is None:
        return OptionalSlot(value, observed_at, verified_now), set()
    if source_restart:
        # Confirmed source restart evidence invalidates optional continuity.
        events.append(EventPlan("optional_rebaselined", observed_at, {"counter": key, "reason": "restart_evidence"}))
        return OptionalSlot(value, observed_at, verified_now), set()
    if value < slot.baseline:
        # Optional decrease resets only this counter (scenario 6).
        events.append(EventPlan("optional_reset", observed_at, {"counter": key, "reason": "counter_reset"}))
        _close_scope(gaps, closed, hour_gaps, scope, observed_at)
        return OptionalSlot(value, observed_at, verified_now), {QualityFlag.RESET_BOUNDARY}
    flags: set[str] = set()
    had_open_gap = scope in gaps
    elapsed = observed_at - slot.observed_at if slot.observed_at is not None else None
    gap_closed = False
    if had_open_gap:
        _close_scope(gaps, closed, hour_gaps, scope, observed_at)
        gap_closed = True
    elif _elapsed_is_gap(elapsed, poll_interval_s):
        start = slot.observed_at
        if start is not None:
            closed.append(ClosedSpan(scope, start, observed_at))
            for hour, seconds, _s, _e in split_span_by_hour(start, observed_at):
                hour_gaps.append(HourGap(scope, hour_label(hour), seconds))
            gap_closed = True
    delta = value - slot.baseline
    if gap_closed:
        flags.update({QualityFlag.COLLECTION_GAP, QualityFlag.TIME_ATTRIBUTION_APPROXIMATE})
    if not verified_now:
        flags.add(QualityFlag.CONTINUITY_UNVERIFIED)
    if slot.observed_at is not None and hour_floor(slot.observed_at) != hour_floor(observed_at):
        flags.add(QualityFlag.TIME_ATTRIBUTION_APPROXIMATE)
    eligible = bool(
        not gap_closed
        and elapsed is not None
        and elapsed > timedelta(0)
        and verified_now
        and slot.baseline is not None
    )
    hour_deltas.append(
        HourDelta(
            key,
            hour_label(observed_at),
            delta,
            1,
            observed_at=observed_at,
            interval_seconds=None if elapsed is None else elapsed.total_seconds(),
            eligible=eligible,
            quality=frozenset(flags),
        )
    )
    return OptionalSlot(value, observed_at, verified_now), flags


def transition_series(
    state: SeriesState | None,
    scrape: ScrapeCounters | None,
    observed_at: datetime,
    stats: StatsEvidence | None,
    poll_interval_s: int,
    *,
    new_epoch_hint: EpochReason | None = None,
    labels: dict[str, str] | None = None,
) -> SeriesPlan:
    """Compute one poll's durable transition for a single series identity.

    ``labels`` must carry the persisted label set when ``scrape`` is None
    (series absent from the scrape); it is ignored when a scrape is present.
    """
    labels = scrape.labels if scrape is not None else (labels or {})
    hour_gaps: list[HourGap] = []
    closed: list[ClosedSpan] = []
    opened: list[OpenGap] = []
    events: list[EventPlan] = []
    hour_deltas: list[HourDelta] = []
    gaps: dict[str, OpenGap] = dict(state.open_gaps) if state is not None else {}
    new_epoch: NewEpoch | None = None
    sample: SamplePlan | None = None

    # ------------------------------------------------------------------
    # Series absent from a valid scrape: gaps, not zeros; baseline retained.
    # ------------------------------------------------------------------
    if scrape is None:
        assert state is not None, "absence of an unknown series is not a transition"
        for scope, last_usable in _scope_last_usable(state):
            gap = _open_scope(gaps, scope, last_usable)
            if gap is not None:
                opened.append(gap)
        events.append(EventPlan("series_absent", observed_at, {}))
        new_state = replace(state, open_gaps=gaps, stale_since=state.stale_since or observed_at)
        return SeriesPlan(labels, False, None, None, new_state, [], [], [], opened, [EventPlan("series_absent", observed_at)])

    counters = scrape.values
    problems = scrape.problems
    primary_usable = scrape.primary_usable()
    ci = counters.get(COMPUTED_INPUT)
    go = counters.get(GENERATED_OUTPUT)

    applies = _evidence_applies(labels, stats)
    interval = _evidence_of(stats) if applies and stats is not None else None
    prev_interval = _state_interval(state) if state is not None else None
    restart_suspect = (
        state is not None
        and interval is not None
        and prev_interval is not None
        and StatsEvidence.intersect(prev_interval, interval) is None
    )
    # Continuity is verified only through an unbroken chain of matching
    # engine-0 uptime evidence; engine 1 never borrows engine 0 uptime.
    verified_now = bool(
        applies
        and interval is not None
        and prev_interval is not None
        and StatsEvidence.intersect(prev_interval, interval) is not None
    )
    new_evidence = interval if applies and stats is not None else prev_interval

    if state is None:
        # --------------------------------------------------------------
        # New series: opening balances, zero contribution, no interval.
        # --------------------------------------------------------------
        reason = new_epoch_hint or EpochReason.NEW_SERIES
        new_epoch = NewEpoch(reason, observed_at, ci if primary_usable else None, go if primary_usable else None)
        new_state = SeriesState(
            epoch_id=-1,
            epoch_started_at=observed_at,
            primary=PrimarySlot(
                ci if primary_usable else None,
                go if primary_usable else None,
                observed_at if primary_usable else None,
                verified_now,
                new_evidence[0] if new_evidence else None,
                new_evidence[1] if new_evidence else None,
            ),
            optional={
                key: (
                    OptionalSlot(counters.get(key), observed_at, verified_now)
                    if counters.get(key) is not None
                    else OptionalSlot()
                )
                for key in OPTIONAL_KEYS
            },
            open_gaps={},
            last_accepted_at=observed_at if primary_usable else None,
        )
        if not primary_usable:
            events.append(EventPlan("primary_unusable", observed_at, {k: problems.get(k, "invalid") for k in PRIMARY_KEYS if counters.get(k) is None}))
            sample = SamplePlan(observed_at, dict(counters), {k: None for k in COUNTER_KEYS}, frozenset({QualityFlag.COUNTER_UNAVAILABLE}), False)
        return SeriesPlan(labels, True, new_epoch, sample, new_state, [], [], [], [], events)

    # ------------------------------------------------------------------
    # Wall-clock regression: fresh baseline, no credit, no negative interval.
    # ------------------------------------------------------------------
    if state.last_accepted_at is not None and observed_at <= state.last_accepted_at:
        flags = {QualityFlag.CLOCK_DISCONTINUITY}
        if primary_usable:
            new_epoch = NewEpoch(EpochReason.CLOCK_REGRESSION, observed_at, ci, go)
            for scope, _last in _scope_last_usable(state):
                _close_scope(gaps, closed, hour_gaps, scope, observed_at)
            new_state = SeriesState(
                epoch_id=-1,
                epoch_started_at=observed_at,
                primary=PrimarySlot(ci, go, observed_at, False, None, None),
                optional={key: (OptionalSlot(counters.get(key), observed_at, False) if counters.get(key) is not None else OptionalSlot()) for key in OPTIONAL_KEYS},
                open_gaps=gaps,
                last_accepted_at=observed_at,
                stale_since=None,
            )
            sample = SamplePlan(observed_at, dict(counters), {k: None for k in COUNTER_KEYS}, frozenset(flags), False)
            events.append(EventPlan("clock_discontinuity", observed_at, {}))
        else:
            gap = _open_scope(gaps, PRIMARY_SCOPE, state.primary.observed_at)
            if gap is not None:
                opened.append(gap)
            new_state = replace(state, open_gaps=gaps)
            events.append(EventPlan("clock_discontinuity", observed_at, {"primary": "unusable"}))
        return SeriesPlan(labels, False, new_epoch, sample, new_state, [], [], closed, opened, events)

    # ------------------------------------------------------------------
    # Unusable primary pair: retain the baseline, open the primary gap.
    # Optional counters remain independent (scenario 15).
    # ------------------------------------------------------------------
    if not primary_usable:
        gap = _open_scope(gaps, PRIMARY_SCOPE, state.primary.observed_at)
        if gap is not None:
            opened.append(gap)
        optionals, optional_flags = _advance_optionals(state, counters, observed_at, gaps, closed, hour_gaps, events, hour_deltas, poll_interval_s, verified_now, source_restart=False)
        deltas = {key: None for key in COUNTER_KEYS}
        for hd in hour_deltas:
            deltas[hd.counter_key] = hd.tokens
        quality = {QualityFlag.COUNTER_UNAVAILABLE} | optional_flags
        if gap is not None:
            quality.add(QualityFlag.COLLECTION_GAP)
        sample = SamplePlan(observed_at, dict(counters), deltas, frozenset(quality), False)
        events.append(EventPlan("primary_unusable", observed_at, {k: problems.get(k, "invalid") for k in PRIMARY_KEYS if counters.get(k) is None}))
        new_state = replace(state, optional=optionals, open_gaps=gaps, stale_since=None)
        return SeriesPlan(labels, False, None, sample, new_state, hour_deltas, hour_gaps, closed, opened, events)

    # ------------------------------------------------------------------
    # No committed baseline yet (endpoint change / prior primary unusable):
    # establish the opening balance; the baseline contributes nothing.
    # ------------------------------------------------------------------
    if (
        new_epoch_hint is EpochReason.ENDPOINT_CHANGE
        or state.primary.observed_at is None
        or state.primary.computed_input is None
        or state.primary.generated_output is None
    ):
        reason = new_epoch_hint or EpochReason.NEW_SERIES
        new_epoch = NewEpoch(reason, observed_at, ci, go)
        for scope, _last in _scope_last_usable(state):
            _close_scope(gaps, closed, hour_gaps, scope, observed_at)
        new_state = SeriesState(
            epoch_id=-1,
            epoch_started_at=observed_at,
            primary=PrimarySlot(ci, go, observed_at, verified_now,
                                 new_evidence[0] if new_evidence else None,
                                 new_evidence[1] if new_evidence else None),
            optional=_rebaselined_optionals(state, counters, observed_at, verified_now),
            open_gaps=gaps,
            last_accepted_at=observed_at,
            stale_since=None,
        )
        sample = SamplePlan(observed_at, dict(counters), {k: None for k in COUNTER_KEYS}, frozenset(), False)
        return SeriesPlan(labels, False, new_epoch, sample, new_state, [], [], closed, opened, events)

    # ------------------------------------------------------------------
    # Disjoint process-start evidence: conservative new epoch, no credit.
    # ------------------------------------------------------------------
    if restart_suspect:
        new_epoch = NewEpoch(EpochReason.RESTART_EVIDENCE, observed_at, ci, go)
        for scope, _last in _scope_last_usable(state):
            _close_scope(gaps, closed, hour_gaps, scope, observed_at)
        events.append(EventPlan("restart_detected", observed_at, {"evidence": "disjoint_uptime_intervals"}))
        new_state = SeriesState(
            epoch_id=-1,
            epoch_started_at=observed_at,
            primary=PrimarySlot(ci, go, observed_at, True, interval[0], interval[1]),
            optional=_rebaselined_optionals(state, counters, observed_at, True),
            open_gaps=gaps,
            last_accepted_at=observed_at,
            stale_since=None,
        )
        sample = SamplePlan(
            observed_at,
            dict(counters),
            {k: None for k in COUNTER_KEYS},
            frozenset({QualityFlag.RESET_BOUNDARY}),
            False,
        )
        return SeriesPlan(labels, False, new_epoch, sample, new_state, [], [], closed, opened, events)

    # ------------------------------------------------------------------
    # Primary decrease: coupled reset of both engine baselines.
    # ------------------------------------------------------------------
    prev_ci = state.primary.computed_input
    prev_go = state.primary.generated_output
    assert prev_ci is not None and prev_go is not None
    if ci < prev_ci or go < prev_go:
        new_epoch = NewEpoch(EpochReason.COUNTER_RESET, observed_at, ci, go)
        for scope, _last in _scope_last_usable(state):
            _close_scope(gaps, closed, hour_gaps, scope, observed_at)
        events.append(EventPlan("counter_reset", observed_at, {"reason": "primary_decrease"}))
        # Counter resets alone never reset optional baselines (scenario 6).
        optionals, optional_flags = _advance_optionals(state, counters, observed_at, gaps, closed, hour_gaps, events, hour_deltas, poll_interval_s, verified_now, source_restart=False)
        new_state = SeriesState(
            epoch_id=-1,
            epoch_started_at=observed_at,
            primary=PrimarySlot(ci, go, observed_at, verified_now,
                                 new_evidence[0] if new_evidence else None,
                                 new_evidence[1] if new_evidence else None),
            optional=optionals,
            open_gaps=gaps,
            last_accepted_at=observed_at,
            stale_since=None,
        )
        deltas = {COMPUTED_INPUT: None, GENERATED_OUTPUT: None}
        deltas.update({hd.counter_key: hd.tokens for hd in hour_deltas})
        sample = SamplePlan(
            observed_at,
            dict(counters),
            deltas,
            frozenset({QualityFlag.RESET_BOUNDARY} | optional_flags),
            False,
        )
        return SeriesPlan(labels, False, new_epoch, sample, new_state, hour_deltas, hour_gaps, closed, opened, events)

    # ------------------------------------------------------------------
    # Normal interval: accepted deltas (including observed zeros).
    # ------------------------------------------------------------------
    flags: set[str] = set()
    elapsed = observed_at - state.primary.observed_at
    gap_closed = _close_scope(gaps, closed, hour_gaps, PRIMARY_SCOPE, observed_at)
    if not gap_closed and _elapsed_is_gap(elapsed, poll_interval_s):
        start = state.primary.observed_at
        closed.append(ClosedSpan(PRIMARY_SCOPE, start, observed_at))
        for hour, seconds, _s, _e in split_span_by_hour(start, observed_at):
            hour_gaps.append(HourGap(PRIMARY_SCOPE, hour_label(hour), seconds))
        gap_closed = True
    if gap_closed:
        flags.update({QualityFlag.COLLECTION_GAP, QualityFlag.TIME_ATTRIBUTION_APPROXIMATE})
    if not verified_now:
        flags.add(QualityFlag.CONTINUITY_UNVERIFIED)
    if hour_floor(state.primary.observed_at) != hour_floor(observed_at):
        flags.add(QualityFlag.TIME_ATTRIBUTION_APPROXIMATE)

    delta_ci = ci - prev_ci
    delta_go = go - prev_go
    hour = hour_label(observed_at)
    # Rates require continuity-verified intervals (specification D4).
    rate_eligible = not gap_closed and elapsed > timedelta(0) and verified_now
    interval_quality = frozenset(flags)
    interval_seconds = elapsed.total_seconds()
    hour_deltas.append(
        HourDelta(
            COMPUTED_INPUT,
            hour,
            delta_ci,
            1,
            observed_at=observed_at,
            interval_seconds=interval_seconds,
            eligible=rate_eligible,
            quality=interval_quality,
        )
    )
    hour_deltas.append(
        HourDelta(
            GENERATED_OUTPUT,
            hour,
            delta_go,
            1,
            observed_at=observed_at,
            interval_seconds=interval_seconds,
            eligible=rate_eligible,
            quality=interval_quality,
        )
    )

    optionals, optional_flags = _advance_optionals(state, counters, observed_at, gaps, closed, hour_gaps, events, hour_deltas, poll_interval_s, verified_now, source_restart=False)
    flags |= optional_flags

    deltas = {COMPUTED_INPUT: delta_ci, GENERATED_OUTPUT: delta_go}
    deltas.update({hd.counter_key: hd.tokens for hd in hour_deltas if hd.counter_key in OPTIONAL_KEYS})
    sample = SamplePlan(observed_at, dict(counters), deltas, frozenset(flags), rate_eligible)
    new_state = SeriesState(
        epoch_id=state.epoch_id,
        epoch_started_at=state.epoch_started_at,
        primary=PrimarySlot(ci, go, observed_at, verified_now,
                             new_evidence[0] if new_evidence else None,
                             new_evidence[1] if new_evidence else None),
        optional=optionals,
        open_gaps=gaps,
        last_accepted_at=observed_at,
        stale_since=None,
    )
    return SeriesPlan(labels, False, None, sample, new_state, hour_deltas, hour_gaps, closed, opened, events)


def _scope_last_usable(state: SeriesState) -> list[tuple[str, datetime | None]]:
    return [
        (PRIMARY_SCOPE, state.primary.observed_at),
        *[(key, state.optional.get(key, OptionalSlot()).observed_at) for key in OPTIONAL_KEYS],
    ]


def _optional_slot_for(state, key, counters, gaps, closed, hour_gaps, events, hour_deltas, poll_interval_s, verified_now, source_restart, observed_at) -> tuple[OptionalSlot, set[str]]:
    return _optional_transition(
        key,
        state.optional.get(key, OptionalSlot()),
        counters.get(key),
        observed_at,
        gaps,
        closed,
        hour_gaps,
        events,
        hour_deltas,
        poll_interval_s,
        verified_now,
        source_restart,
    )


def _rebaselined_optionals(state: SeriesState, counters: dict[str, int | None], observed_at: datetime, verified: bool) -> dict[str, OptionalSlot]:
    """Establish fresh optional baselines with no credit (restart/new epoch)."""
    return {
        key: (OptionalSlot(counters.get(key), observed_at, verified) if counters.get(key) is not None else OptionalSlot())
        for key in OPTIONAL_KEYS
    }


def _advance_optionals(state, counters, observed_at, gaps, closed, hour_gaps, events, hour_deltas, poll_interval_s, verified_now, source_restart) -> tuple[dict[str, OptionalSlot], set[str]]:
    slots: dict[str, OptionalSlot] = {}
    flags: set[str] = set()
    for key in OPTIONAL_KEYS:
        slot, slot_flags = _optional_slot_for(state, key, counters, gaps, closed, hour_gaps, events, hour_deltas, poll_interval_s, verified_now, source_restart, observed_at)
        slots[key] = slot
        flags |= slot_flags
    return slots, flags
