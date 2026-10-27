"""Live engine stats: extraction, in-memory windows, and the snapshot the API serves.

Two upstream sources feed the live row (specification D10, live-panel plan D3/D4):
``/stats`` carries the instantaneous gauges Radiance reports (running, waiting,
TPS) plus the cumulative expert-routing pair; ``/metrics`` carries the cumulative
draft pair. Nothing here touches SQLite — a dashboard restart clears every value.

The window engine is pure: every call takes the monotonic clock and the wall
clock as arguments, so the 5 s / 1 m / 30 m fill boundaries, the reset rules, and
the staleness rule are provable without waiting for real time. ``LiveMonitor``
(step 2) is the only impure part: it fetches, records, and publishes.

Ratio convention: values are fractions in 0..1 with ``None`` meaning "render
``-``". A window never carries a value over from an earlier run and never
reports ``0`` for a denominator that did not advance.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Config
from .domain import to_iso, utc_now
from .metrics import (
    CounterValueError,
    UpstreamError,
    UpstreamFetcher,
    _scan_sample_lines,
    parse_exact_integer_lexeme,
)

# Live cadence and window spans (plan D2/D4). Fixed by design: no new
# environment variables — a glance panel has exactly one sensible refresh rate.
LIVE_CADENCE_SECONDS = 1.0
WINDOW_SECONDS: tuple[tuple[str, float], ...] = (
    ("window_5s", 5.0),
    ("window_1m", 60.0),
    ("window_30m", 1800.0),
)
# A run ends when a source is silent for *more* than this long, and its tiles go
# null on the same threshold (plan D4: prefer "-" over a stale number).
GAP_SECONDS = 3.0
FRESHNESS_SECONDS = 3.0
# A window's baseline may not be older than its span plus this tolerance: a
# sample gap in the middle of a run must not silently widen the window.
WINDOW_TOLERANCE_SECONDS = 2.0
# Longest window plus scheduling slack; bounds the in-memory run.
RETENTION_SECONDS = WINDOW_SECONDS[-1][1] + 30.0

STATS_SOURCE = "stats"
METRICS_SOURCE = "metrics"
SOURCES: tuple[str, ...] = (STATS_SOURCE, METRICS_SOURCE)

DRAFT_TOKENS_METRIC = "radiance:draft_tokens_total"
DRAFT_ACCEPTED_METRIC = "radiance:draft_accepted_total"
_DRAFT_METRICS = (DRAFT_TOKENS_METRIC, DRAFT_ACCEPTED_METRIC)

STATE_STARTING = "starting"
STATE_LIVE = "live"
STATE_STALE = "stale"

# The poller's own fetch budget: the loop must fit inside its 1 s slot (plan D1).
LIVE_FETCH_TIMEOUT_CEILING_SECONDS = 0.9
# The live thread is joined on every exit path; it never gates shutdown.
LIVE_JOIN_BUDGET_SECONDS = 1.0

log = logging.getLogger("jarvis_dashboard.live")


# --------------------------------------------------------------------------- #
# Extraction (plan D3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StatsSample:
    """One ``/stats`` reading: the gauges plus the cumulative expert pair.

    ``uptime_s`` is the restart anchor for the source's run and therefore the
    only mandatory field. Every other field is ``None`` when missing, of the
    wrong type, non-finite, or negative — and nullifies only its own tile.
    """

    uptime_s: float
    running: int | None = None
    waiting: int | None = None
    prefill_tps: float | None = None
    decode_tps: float | None = None
    routed: float | None = None
    routed_res: float | None = None

    def monotonic_fields(self) -> dict[str, float | None]:
        """Cumulative fields: a decrease proves a Radiance restart (plan D4).

        The gauges (running, waiting, TPS) legitimately fall, so they are not
        part of reset detection.
        """
        return {"uptime_s": self.uptime_s, "routed": self.routed, "routed_res": self.routed_res}

    def expert_pair(self) -> tuple[float, float] | None:
        """(routed, routed_res) — a ratio needs both operands, never one."""
        if self.routed is None or self.routed_res is None:
            return None
        return self.routed, self.routed_res


@dataclass(frozen=True)
class DraftSample:
    """One ``/metrics`` reading of the MTP draft pair; ``None`` = unavailable.

    Unavailable means the exposition is ambiguous for this poll: a name with
    zero samples, a name with more than one sample (several label sets are
    never summed, specification D3), an invalid lexeme, or an over-large body.
    """

    draft_tokens: int | None = None
    draft_accepted: int | None = None

    def monotonic_fields(self) -> dict[str, float | None]:
        return {"draft_tokens": self.draft_tokens, "draft_accepted": self.draft_accepted}

    def pair(self) -> tuple[int, int] | None:
        if self.draft_tokens is None or self.draft_accepted is None:
            return None
        return self.draft_tokens, self.draft_accepted


def _as_count(value: Any) -> int | None:
    """Non-negative integer gauge; booleans are not counts."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _as_rate(value: Any) -> float | None:
    """Non-negative finite rate."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rate = float(value)
    if not math.isfinite(rate) or rate < 0:
        return None
    return rate


def parse_stats_document(body: bytes) -> StatsSample:
    """Extract the live ``/stats`` fields from one response body.

    Structural failures (unparseable JSON, a non-object document, a missing or
    invalid ``uptime_s``) raise :class:`UpstreamError` with the same codes the
    durable collector reports: without a trustworthy uptime the run boundary is
    unknowable, so the whole poll is a failure. Per-field problems are
    nulls, never failures.
    """
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpstreamError("stats_invalid_json") from exc
    if not isinstance(document, dict):
        raise UpstreamError("stats_invalid_shape")
    uptime = document.get("uptime_s")
    if (
        isinstance(uptime, bool)
        or not isinstance(uptime, (int, float))
        or not math.isfinite(uptime)
        or uptime < 0
    ):
        raise UpstreamError("stats_invalid_uptime")
    routed = routed_res = None
    experts = document.get("experts")
    if isinstance(experts, dict):
        pair_routed = _as_count(experts.get("routed"))
        pair_res = _as_count(experts.get("routed_res"))
        if pair_routed is not None and pair_res is not None:
            routed, routed_res = float(pair_routed), float(pair_res)
    return StatsSample(
        uptime_s=float(uptime),
        running=_as_count(document.get("running")),
        waiting=_as_count(document.get("waiting")),
        prefill_tps=_as_rate(document.get("prefill_tps")),
        decode_tps=_as_rate(document.get("decode_tps")),
        routed=routed,
        routed_res=routed_res,
    )


def parse_draft_document(text: str) -> DraftSample:
    """Extract the draft pair from exposition text with the shared line scanner.

    Two names, one sample each — deliberately no full family parse at 1 Hz.
    Any ambiguity yields an unavailable group rather than a summed guess.
    """
    counts: dict[str, int] = {}
    lexemes: dict[str, str] = {}
    try:
        samples = _scan_sample_lines(text)
    except ValueError:
        # Body larger than the scrape bound: unparseable at 1 Hz, so unavailable.
        return DraftSample()
    for raw in samples:
        if raw.name not in _DRAFT_METRICS:
            continue
        counts[raw.name] = counts.get(raw.name, 0) + 1
        lexemes.setdefault(raw.name, raw.value_lexeme)
    if any(counts.get(name, 0) != 1 for name in _DRAFT_METRICS):
        return DraftSample()
    try:
        return DraftSample(
            draft_tokens=parse_exact_integer_lexeme(lexemes[DRAFT_TOKENS_METRIC]),
            draft_accepted=parse_exact_integer_lexeme(lexemes[DRAFT_ACCEPTED_METRIC]),
        )
    except CounterValueError:
        return DraftSample()


# --------------------------------------------------------------------------- #
# Window engine (plan D4)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Point:
    """One successful sample in a source's current run."""

    at: float
    wall: datetime
    sample: StatsSample | DraftSample


@dataclass
class _SourceRun:
    """Consecutive trustworthy samples for one source, reset as a whole."""

    points: deque[_Point]
    last_success: float | None = None
    last_wall: datetime | None = None

    def __init__(self) -> None:
        self.points = deque()
        self.last_success = None
        self.last_wall = None

    def last(self) -> _Point | None:
        return self.points[-1] if self.points else None

    def record(self, at: float, wall: datetime, sample: StatsSample | DraftSample) -> bool:
        """Append one sample; True when this sample started a new run."""
        previous = self.last()
        restarted = previous is None or _ends_run(previous.at, at, previous.sample, sample)
        if restarted:
            self.points.clear()
        self.points.append(_Point(at, wall, sample))
        while self.points and at - self.points[0].at > RETENTION_SECONDS:
            self.points.popleft()
        self.last_success = at
        self.last_wall = wall
        return restarted

    def fresh(self, now: float) -> bool:
        return self.last_success is not None and now - self.last_success <= FRESHNESS_SECONDS


def _ends_run(
    last_at: float, at: float, previous: StatsSample | DraftSample, current: StatsSample | DraftSample
) -> bool:
    """A gap over ``GAP_SECONDS`` or any cumulative decrease ends the run (plan D4)."""
    return at - last_at > GAP_SECONDS or _counters_decreased(previous, current)


def _counters_decreased(previous: StatsSample | DraftSample, current: StatsSample | DraftSample) -> bool:
    """True when a cumulative field fell: a restart or an upstream counter reset."""
    for name, value in current.monotonic_fields().items():
        before = previous.monotonic_fields().get(name)
        if value is not None and before is not None and value < before:
            return True
    return False


def _windowed_ratio(points: list[tuple[float, float, float]], span: float, tolerance: float) -> float | None:
    """Δnumerator / Δdenominator over the newest baseline at least ``span`` old.

    ``points`` are ``(at, numerator, denominator)`` in ascending time order and
    all belong to one run. ``None`` when no baseline exists yet, when the only
    baseline is older than ``span + tolerance``, when the denominator did not
    advance, or when the numerator moved backwards.
    """
    if not points:
        return None
    end_at, end_num, end_den = points[-1]
    for at, num, den in reversed(points):
        age = end_at - at
        if age < span:
            continue
        if age > span + tolerance:
            return None
        den_delta = end_den - den
        num_delta = end_num - num
        if den_delta <= 0 or num_delta < 0:
            return None
        return num_delta / den_delta
    return None


class LiveWindowEngine:
    """Runs, windows, freshness, and the snapshot — all clock-injected (plan D4).

    Each source owns its run: a ``/stats`` restart never clears the MTP windows
    and a ``/metrics`` gap never clears the MoE windows.
    """

    def __init__(self) -> None:
        self._runs: dict[str, _SourceRun] = {name: _SourceRun() for name in SOURCES}
        self._ever_live = False

    # -- recording -----------------------------------------------------------

    def record_stats(self, at: float, wall: datetime, sample: StatsSample) -> bool:
        """Record a trustworthy ``/stats`` reading; True when the run restarted."""
        return self._runs[STATS_SOURCE].record(at, wall, sample)

    def record_metrics(self, at: float, wall: datetime, sample: DraftSample) -> bool:
        """Record a trustworthy ``/metrics`` reading; True when the run restarted."""
        return self._runs[METRICS_SOURCE].record(at, wall, sample)

    # -- queries -------------------------------------------------------------

    def source_fresh(self, source: str, now: float) -> bool:
        return self._runs[source].fresh(now)

    def moe_available(self, now: float) -> bool:
        """Latest fresh ``/stats`` sample carries a usable expert pair."""
        run = self._runs[STATS_SOURCE]
        if not run.fresh(now):
            return False
        for point in reversed(run.points):
            if isinstance(point.sample, StatsSample):
                return point.sample.expert_pair() is not None
        return False

    def mtp_available(self, now: float) -> bool:
        """Latest fresh ``/metrics`` sample carries a usable draft pair."""
        run = self._runs[METRICS_SOURCE]
        if not run.fresh(now):
            return False
        for point in reversed(run.points):
            if isinstance(point.sample, DraftSample):
                return point.sample.pair() is not None
        return False

    # -- snapshot ------------------------------------------------------------

    def snapshot(self, now: float, wall_now: datetime) -> dict[str, Any]:
        """The immutable document ``GET /api/v1/live`` serves (plan D5)."""
        stats_run = self._runs[STATS_SOURCE]
        metrics_run = self._runs[METRICS_SOURCE]
        stats_fresh = stats_run.fresh(now)
        metrics_fresh = metrics_run.fresh(now)

        stats = _last_stats(stats_run, stats_fresh)
        requests = {
            "active": stats.running if stats is not None else None,
            "queued": stats.waiting if stats is not None else None,
        }
        prefill = stats.prefill_tps if stats is not None else None
        decode = stats.decode_tps if stats is not None else None
        per_request = _per_request(decode, stats.running if stats is not None else None)

        moe_points = _expert_points(stats_run, stats_fresh)
        mtp_points = _draft_points(metrics_run, metrics_fresh)
        moe = {label: _windowed_ratio(moe_points, span, WINDOW_TOLERANCE_SECONDS) for label, span in WINDOW_SECONDS}
        mtp = {label: _windowed_ratio(mtp_points, span, WINDOW_TOLERANCE_SECONDS) for label, span in WINDOW_SECONDS}
        mtp["lifetime"] = _lifetime(metrics_run, metrics_fresh)

        if stats_fresh or metrics_fresh:
            self._ever_live = True
            state = STATE_LIVE
            observed_at = _latest_wall(stats_run, metrics_run, stats_fresh, metrics_fresh)
        else:
            state = STATE_STALE if self._ever_live else STATE_STARTING
            observed_at = None

        return {
            "state": state,
            "observed_at": to_iso(observed_at) if observed_at is not None else None,
            "requests": requests,
            "prefill_tokens_per_second": prefill,
            "decode_tokens_per_second": decode,
            "decode_tokens_per_second_per_request": per_request,
            "mtp_acceptance": mtp,
            "moe_gpu_hit": moe,
        }


def _expert_pair(sample: StatsSample | DraftSample) -> tuple[float, float] | None:
    return sample.expert_pair() if isinstance(sample, StatsSample) else None


def _draft_pair(sample: StatsSample | DraftSample) -> tuple[int, int] | None:
    return sample.pair() if isinstance(sample, DraftSample) else None


def _last_stats(run: _SourceRun, fresh: bool) -> StatsSample | None:
    if not fresh:
        return None
    for point in reversed(run.points):
        if isinstance(point.sample, StatsSample):
            return point.sample
    return None


def _expert_points(run: _SourceRun, fresh: bool) -> list[tuple[float, float, float]]:
    """(at, routed_res, routed) for the samples of a fresh run that carry the pair."""
    if not fresh:
        return []
    points: list[tuple[float, float, float]] = []
    for point in run.points:
        pair = _expert_pair(point.sample)
        if pair is not None:
            points.append((point.at, pair[1], pair[0]))
    return points


def _draft_points(run: _SourceRun, fresh: bool) -> list[tuple[float, float, float]]:
    """(at, draft_accepted, draft_tokens) for the samples of a fresh run that carry the pair."""
    if not fresh:
        return []
    points: list[tuple[float, float, float]] = []
    for point in run.points:
        pair = _draft_pair(point.sample)
        if pair is not None:
            points.append((point.at, float(pair[1]), float(pair[0])))
    return points


def _per_request(decode_tps: float | None, running: int | None) -> float | None:
    """Decode rate per active request; no requests means no per-request number."""
    if decode_tps is None or running is None or running < 1:
        return None
    return decode_tps / running


def _lifetime(run: _SourceRun, fresh: bool) -> float | None:
    """Accepted ÷ drafted from the latest cumulative reading (lifetime, not window)."""
    if not fresh:
        return None
    for point in reversed(run.points):
        pair = _draft_pair(point.sample)
        if pair is None:
            continue
        tokens, accepted = pair
        return accepted / tokens if tokens > 0 else None
    return None


def _latest_wall(
    stats_run: _SourceRun,
    metrics_run: _SourceRun,
    stats_fresh: bool,
    metrics_fresh: bool,
) -> datetime | None:
    walls = [
        run.last_wall
        for run, fresh in ((stats_run, stats_fresh), (metrics_run, metrics_fresh))
        if fresh and run.last_wall is not None
    ]
    return max(walls) if walls else None


# --------------------------------------------------------------------------- #
# Poller thread (plan D1/D2)
# --------------------------------------------------------------------------- #

STATS_PATH = "/stats"
METRICS_PATH = "/metrics"


class LiveMonitor:
    """The 1 Hz live poller: fetch, record, publish. Never touches SQLite.

    Owned by ``run_serve`` only (plan D1) — the collector owns the writer thread
    and the 15 s schedule, and readiness must not depend on this thread. The
    loop keeps one fetch in flight, runs ``/stats`` then ``/metrics``
    sequentially, and skips missed slots instead of queueing them. Logging is
    transition-only: a failing upstream must not print a line per second.
    """

    def __init__(
        self,
        config: Config,
        *,
        fetcher: UpstreamFetcher | None = None,
        engine: LiveWindowEngine | None = None,
        wall_clock=utc_now,
        monotonic=time.monotonic,
        sleeper=None,
    ) -> None:
        # The whole cycle (two fetches plus the snapshot) must fit the 1 s slot (plan D2).
        self.fetch_timeout_seconds = min(
            config.http_timeout_seconds, LIVE_FETCH_TIMEOUT_CEILING_SECONDS
        )
        self._fetcher = fetcher or UpstreamFetcher(
            config.radiance_base_url,
            self.fetch_timeout_seconds,
            wall_clock=wall_clock,
            monotonic=monotonic,
        )
        self._engine = engine or LiveWindowEngine()
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._sleep = sleeper if sleeper is not None else self._stop_wait
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] = self._engine.snapshot(monotonic(), wall_clock())
        # Transition-only logging state (plan D2/D6).
        self._state = self._snapshot["state"]
        self._fresh: dict[str, bool] = {name: False for name in SOURCES}
        self._fields = {"experts": False, "draft": False}
        self._last_error: dict[str, str | None] = {name: None for name in SOURCES}
        self._seen: dict[str, bool] = {name: False for name in SOURCES}
        self._missing_slots = False

    # -- control -------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon poller; a no-op when already running."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="jarvis-live", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = LIVE_JOIN_BUDGET_SECONDS) -> bool:
        """True when the thread ended within ``timeout`` (it never gates shutdown)."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.error("live poller exceeded the shutdown budget")
            return False
        return True

    def stopped(self) -> bool:
        return self._stop.is_set()

    def snapshot(self) -> dict[str, Any]:
        """The latest published snapshot; the API handler returns it as-is."""
        return self._snapshot

    def _stop_wait(self, timeout: float) -> bool:
        return self._stop.wait(timeout)

    # -- loop ----------------------------------------------------------------

    def _run(self) -> None:
        next_slot = self._monotonic()
        while not self._stop.is_set():
            self.poll_once()
            # Monotonic schedule: a slow cycle skips slots, it never bursts (plan D2).
            next_slot += LIVE_CADENCE_SECONDS
            now = self._monotonic()
            behind = next_slot <= now
            if behind:
                missed = int((now - next_slot) // LIVE_CADENCE_SECONDS) + 1
                next_slot += missed * LIVE_CADENCE_SECONDS
            if behind != self._missing_slots:
                # Transition-only: a slow upstream misses every slot at 1 Hz.
                self._missing_slots = behind
                if behind:
                    log.info("live poller is falling behind; skipped %d slot(s)", missed)
                else:
                    log.info("live poller back on schedule")
            self._sleep(max(0.0, next_slot - self._monotonic()))

    def poll_once(self) -> None:
        """One cycle: ``/stats`` then ``/metrics``, then publish (success or not)."""
        self._poll_stats()
        self._poll_metrics()
        self._publish()

    def _poll_stats(self) -> None:
        at = self._monotonic()
        wall = self._wall_clock()
        try:
            sample = parse_stats_document(self._fetcher.fetch(STATS_PATH))
        except UpstreamError as exc:
            self._last_error[STATS_SOURCE] = exc.code
            return
        self._last_error[STATS_SOURCE] = None
        self._record(STATS_SOURCE, self._engine.record_stats(at, wall, sample))

    def _poll_metrics(self) -> None:
        at = self._monotonic()
        wall = self._wall_clock()
        try:
            text = self._fetcher.fetch(METRICS_PATH).decode("utf-8")
        except UnicodeDecodeError:
            self._last_error[METRICS_SOURCE] = "metrics_invalid_utf8"
            return
        except UpstreamError as exc:
            self._last_error[METRICS_SOURCE] = exc.code
            return
        sample = parse_draft_document(text)
        self._last_error[METRICS_SOURCE] = None
        self._record(METRICS_SOURCE, self._engine.record_metrics(at, wall, sample))

    def _record(self, source: str, restarted: bool) -> None:
        if restarted and self._seen[source]:
            # One line per reset event: uptime drop, counter drop, or a >3 s gap.
            log.info("live %s run reset", source)
        self._seen[source] = True

    def _publish(self) -> None:
        now = self._monotonic()
        snapshot = self._engine.snapshot(now, self._wall_clock())
        self._log_transitions(snapshot, now)
        self._snapshot = snapshot

    def _log_transitions(self, snapshot: dict[str, Any], now: float) -> None:
        state = snapshot["state"]
        if state != self._state:
            log.info("live state %s -> %s", self._state, state)
            self._state = state
        for source in SOURCES:
            fresh = self._engine.source_fresh(source, now)
            if fresh == self._fresh[source]:
                continue
            self._fresh[source] = fresh
            if fresh:
                log.info("live source %s recovered", source)
            else:
                log.warning(
                    "live source %s went stale",
                    source,
                    extra={"error_code": self._last_error[source] or ""},
                )
        for name, available in (
            ("experts", self._engine.moe_available(now)),
            ("draft", self._engine.mtp_available(now)),
        ):
            if available == self._fields[name]:
                continue
            self._fields[name] = available
            log.info("live field %s %s", name, "present" if available else "absent")
