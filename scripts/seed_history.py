#!/usr/bin/env python3
"""Deterministic one-year history seeder (docs/delivery-4-implementation-plan.md Step 4, D5).

Drives the real ``Collector.poll_once`` against an in-process fixture on a
simulated UTC clock, so a full year of history is written through the production
writer invariants (coverage metadata, gap rows, latest interval) instead of
hand-written SQL. The result preconditiones AC3 and AC6.

Contract (D5):
- Window: UTC midnight ``window_days`` days before the run up to the real now.
- Cadence: ``cadence_early`` seconds (300) until day -30, then production 15 s.
- ``--base-url`` / ``--source-id`` equal the container's, so no endpoint-change
  epoch is created when the image later runs against the seeded volume.
- Traffic: deterministic diurnal model from ``--seed``, including idle hours.
- Designed cases (D5 table) are recorded in the manifest with their timestamps.
- Finish: drain gap materialization, ``purge_expired`` at now - retention,
  ``wal_checkpoint(TRUNCATE)`` and ``integrity_check``.
- The manifest carries expected lifetime/day totals per counter, final counters,
  the process-start origin and the designed case timestamps. Seeding is never
  part of a measured window.

``--verify`` serves the seeded volume with the real ``serve`` from this tree and
compares API lifetime, 366-day daily history, the midnight hourly cases, the
restart-evidence epoch, raw retention and ``integrity_check`` against the
manifest (Step 4 verification).

Usage:
  .venv/bin/python scripts/seed_history.py --db DIR/dashboard.sqlite3 \
      --base-url http://127.0.0.1:0 --source-id qual-jarvis --seed 20260426 \
      --manifest DIR/manifest.json
  .venv/bin/python scripts/seed_history.py --verify --db ... --manifest ...
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from fixture_server import FixtureServer, install_fixture  # noqa: E402
from jarvis_dashboard import storage  # noqa: E402
from jarvis_dashboard.collector import Collector  # noqa: E402
from jarvis_dashboard.config import DEFAULT_RAW_RETENTION_DAYS, Config  # noqa: E402
from jarvis_dashboard.domain import (  # noqa: E402
    CACHED_INPUT,
    COUNTER_KEYS,
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    REQUEST_INPUT,
    REQUEST_OUTPUT,
    to_iso,
)

sys.path.insert(0, str(ROOT / "scripts"))
import qualify_common as qual  # noqa: E402

METRIC_FOR_KEY = {
    COMPUTED_INPUT: "radiance:prefill_tokens_total",
    GENERATED_OUTPUT: "radiance:decode_tokens_total",
    REQUEST_INPUT: "vllm:prompt_tokens_total",
    REQUEST_OUTPUT: "vllm:generation_tokens_total",
}
# Diurnal weight by UTC hour, then a per-model scale and per-counter ratio:
# the same seed must produce the same history on every host.
DIURNAL = (
    0.15, 0.10, 0.08, 0.06, 0.06, 0.10, 0.35, 0.70, 1.00, 1.25, 1.40, 1.30,
    1.00, 1.15, 1.35, 1.40, 1.30, 1.10, 0.85, 0.70, 0.60, 0.50, 0.35, 0.25,
)
# cached_input stays well below request_input so the cost layer never sees a cache
# hit larger than the request it belongs to.
COUNTER_RATIO = {COMPUTED_INPUT: 1.0, GENERATED_OUTPUT: 0.07, REQUEST_INPUT: 0.9, REQUEST_OUTPUT: 0.06,
                 CACHED_INPUT: 0.35}
IDLE_HOUR_PROBABILITY = 0.04
MODEL_COUNT = 8
# The model whose engine-0 uptime evidence Radiance `/stats` reports (D5 restart case).
STATS_MODEL_INDEX = 0
# D5 designed-case placement, in UTC days before the most recent midnight.
DEFAULT_CASE_DAYS = {"primary_reset": 40, "stats_only": 100, "request_absent": 150,
                     "idle_day": 20, "collector_downtime": 60}
TOKEN_FLOOR = 240.0  # tokens/minute at weight 1.0, model 0: a shared engine's busy hour


def model_labels(index: int) -> dict[str, str]:
    return {"model_name": f"qual-model-{index}", "engine": "0"}


@dataclass(frozen=True)
class Case:
    key: str
    start: datetime
    end: datetime
    note: str

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def build_cases(midnight: datetime, days_ago: dict[str, int]) -> list[Case]:
    """The D5 designed cases, anchored to the two most recent UTC midnights."""
    day = timedelta(days=1)
    return [
        Case("outage_compatible", midnight - timedelta(minutes=65), midnight + timedelta(minutes=20),
             "upstream /metrics outage across the most recent midnight, recovered with compatible uptime"),
        Case("outage_restart", midnight - day - timedelta(minutes=65), midnight - day + timedelta(minutes=30),
             "outage across the previous midnight with changed uptime evidence (restart)"),
        Case("primary_reset", midnight - day * days_ago["primary_reset"] + timedelta(hours=6),
             midnight - day * days_ago["primary_reset"] + timedelta(hours=6, minutes=1),
             "primary counter reset"),
        Case("stats_only", midnight - day * days_ago["stats_only"] + timedelta(hours=9),
             midnight - day * days_ago["stats_only"] + timedelta(hours=15),
             "stats-only outage (engine continuity unverified)"),
        Case("request_absent", midnight - day * days_ago["request_absent"],
             midnight - day * (days_ago["request_absent"] - 7),
             "request counters absent for seven days"),
        Case("idle_day", midnight - day * days_ago["idle_day"], midnight - day * (days_ago["idle_day"] - 1),
             "fully idle day (valid zero strings)"),
        Case("collector_downtime", midnight - day * days_ago["collector_downtime"],
             midnight - day * (days_ago["collector_downtime"] - 1),
             "collector downtime: no polls for a whole UTC day (gap-only null day)"),
    ]


class SimClock:
    """One simulated UTC clock shared by the collector, the fixture and the model."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def wall(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.now.timestamp()

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class TrafficModel:
    """Deterministic per-model counter increments on the simulated timeline."""

    def __init__(self, seed: int, models: list[dict[str, str]]) -> None:
        self.seed = seed
        self.models = models
        self.counters: list[dict[str, int]] = []
        self.frac: list[dict[str, float]] = []
        self.scales: list[float] = []
        for index in range(len(models)):
            rng = random.Random(f"{seed}|model|{index}")
            self.scales.append(TOKEN_FLOOR * (0.6 + 0.9 * rng.random()))
            self.counters.append({key: int(rng.uniform(1_000, 40_000)) for key in COUNTER_KEYS})
            # Sub-token steps must accumulate, not vanish: at a 15 s cadence a
            # quiet hour produces fractions, and dropping them would freeze the
            # counters the last 30 days depend on.
            self.frac.append({key: 0.0 for key in COUNTER_KEYS})

    def advance(self, index: int, moment: datetime, cadence: int, *, primary_reset: bool,
                idle: bool) -> bool:
        """Advance one model's counters for `cadence` seconds; True when reset.

        Counters advance with real simulated time even while the endpoint hides
        them (the request pair during `request_absent`): the dashboard must
        credit the whole hidden span when the series reappears.
        """
        rng = random.Random(f"{self.seed}|model|{index}|{moment:%Y%m%dT%H%M%S}")
        if primary_reset:
            for key in (COMPUTED_INPUT, GENERATED_OUTPUT):
                self.counters[index][key] = 0
            return True
        if idle:
            return False
        weight = DIURNAL[moment.hour] * (0.85 + 0.3 * rng.random())
        if rng.random() < IDLE_HOUR_PROBABILITY:
            weight = 0.0
        for key in COUNTER_KEYS:
            step = self.scales[index] * weight * COUNTER_RATIO[key] * cadence / 60.0 * (0.7 + 0.6 * rng.random())
            accumulated = self.frac[index][key] + step
            whole = int(accumulated)
            self.frac[index][key] = accumulated - whole
            self.counters[index][key] += whole
        return False

    def publish(self, state, index: int, request_absent: bool) -> None:
        """Expose the model's counters; the request pair is absent, never zero."""
        counters = self.counters[index]
        if request_absent:
            install_fixture(state, self.models[index], counters[COMPUTED_INPUT], counters[GENERATED_OUTPUT],
                            None, None, counters[CACHED_INPUT])
            for key in (REQUEST_INPUT, REQUEST_OUTPUT):
                state.clear_metric(METRIC_FOR_KEY[key], self.models[index])
        else:
            install_fixture(state, self.models[index], counters[COMPUTED_INPUT], counters[GENERATED_OUTPUT],
                            counters[REQUEST_INPUT], counters[REQUEST_OUTPUT], counters[CACHED_INPUT])


@dataclass
class Expectation:
    """Expected API-visible totals, built from the traffic model, never from the DB."""

    lifetime: dict[str, int]
    per_model: list[dict[str, int]]
    daily: dict[str, dict[str, int | None]]
    hours: dict[str, dict[str, dict[str, int]]]
    gap_hours: dict[str, str]
    epoch_cases: dict[str, dict[str, str]]


def seed_history(db: Path, base_url: str, source_id: str, seed: int, *, end: datetime,
                 window_days: int, cadence_early: int, cadence_recent: int,
                 retention_days: int, receipts: qual.Receipts,
                 case_days: dict[str, int] | None = None,
                 quiet: bool = False) -> dict:
    started = time.monotonic()
    # The designed outages are simulated failures: the writer's warnings are noise here.
    logging.getLogger("jarvis_dashboard").setLevel(logging.CRITICAL)
    db.parent.mkdir(parents=True, exist_ok=True, mode=0o770)
    os.chmod(db.parent, 0o770)
    if db.exists() and db.stat().st_size > 0:
        raise SystemExit(f"[FAIL] refusing to seed a non-empty database: {db}")

    lock = storage.acquire_volume_lock(db)
    conn = storage.open_storage(db)
    conn.execute("PRAGMA synchronous=OFF")  # D5: verified before use

    clock = SimClock((end - timedelta(days=window_days)).replace(hour=0, minute=0, second=0, microsecond=0))
    origin = clock.now.timestamp()
    models = [model_labels(i) for i in range(MODEL_COUNT)]
    host, port = _split_base_url(base_url)
    # Radiance `/stats` reports one model; engine-0 uptime evidence applies only to
    # that model (accounting._evidence_applies), so the restart case targets it.
    fixture = FixtureServer(model=models[STATS_MODEL_INDEX]["model_name"], host=host, port=port,
                            clock=clock.monotonic).start()
    fixture.state.uptime_origin = origin
    traffic = TrafficModel(seed, models)
    actual_url = fixture.base_url
    window_start = (end - timedelta(days=window_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    cases = build_cases(end.replace(hour=0, minute=0, second=0, microsecond=0),
                        {**DEFAULT_CASE_DAYS, **(case_days or {})})

    collectors: dict[int, Collector] = {}

    def collector_for(cadence: int) -> Collector:
        if cadence not in collectors:
            config = Config.from_env({
                "RADIANCE_BASE_URL": actual_url,
                "SOURCE_ID": source_id,
                "POLL_INTERVAL_SECONDS": str(cadence),
                "DATABASE_PATH": str(db),
                "RAW_RETENTION_DAYS": str(retention_days),
            })
            collectors[cadence] = Collector(
                config, conn, wall_clock=clock.wall, monotonic=clock.monotonic,
                sleeper=lambda _s: None,
            )
        return collectors[cadence]

    expectation = _new_expectation(models, window_start, end)
    last_success: list[dict[str, int]] = [{} for _ in models]
    polls = applied = failed = 0
    last_poll_failed = False
    restart_pending = False
    last_hour_drained = clock.now.replace(minute=0, second=0, microsecond=0)

    while clock.now < end:
        cadence = cadence_recent if clock.now >= end - timedelta(days=30) else cadence_early
        moment = clock.now
        active = {case.key: case for case in cases if case.covers(moment)}
        downtime = "collector_downtime" in active
        if downtime:
            # A whole UTC day without polls has no observations: the API reports
            # null for it, never a zero (D5 designed case).
            gap_day = expectation.daily.get(moment.date().isoformat())
            if gap_day is not None:
                for key in COUNTER_KEYS:
                    gap_day[key] = None
            for index in range(len(models)):
                traffic.advance(index, moment, cadence, primary_reset=False, idle=False)
            clock.advance(cadence)
            continue

        stats_case = "stats_only" in active
        request_absent = "request_absent" in active
        idle_day = "idle_day" in active
        outage_active = "outage_compatible" in active or "outage_restart" in active
        fixture.state.stats_fail = stats_case
        fixture.state.metrics_status = 500 if outage_active else None
        restart_at = None
        if "outage_restart" in active:
            restart_at = active["outage_restart"].start + timedelta(minutes=75)
            if clock.now >= restart_at and fixture.state.uptime_origin != restart_at.timestamp():
                fixture.state.uptime_origin = restart_at.timestamp()
                restart_pending = True

        reset_index: int | None = None
        for index in range(len(models)):
            if traffic.advance(
                index, moment, cadence,
                primary_reset="primary_reset" in active and index == STATS_MODEL_INDEX,
                idle=idle_day,
            ):
                reset_index = index
        if request_absent:
            # Absent series are reported null by the API, never as a zero day.
            absent_day = expectation.daily.get(moment.date().isoformat())
            if absent_day is not None:
                for key in (REQUEST_INPUT, REQUEST_OUTPUT):
                    absent_day[key] = None
        for index in range(len(models)):
            traffic.publish(fixture.state, index, request_absent)

        report = collector_for(cadence).poll_once(f"seed-{polls:07d}")
        polls += 1
        if report.ok:
            applied += report.applied
            _credit(expectation, traffic, last_success, models, moment, reset_index,
                    recovered=last_poll_failed, restart_poll=restart_pending,
                    request_absent=request_absent)
            last_poll_failed = False
            restart_pending = False
        else:
            failed += 1
            last_poll_failed = True
            expectation.gap_hours[f"{moment:%Y-%m-%dT%H}Z"] = "outage_no_sample"
        clock.advance(cadence)

        if clock.now.replace(minute=0, second=0, microsecond=0) != last_hour_drained:
            last_hour_drained = clock.now.replace(minute=0, second=0, microsecond=0)
            while storage.drain_gap_materialization(conn) > 0:
                pass

    while storage.drain_gap_materialization(conn) > 0:
        pass
    removed = storage.purge_expired(conn, to_iso(end - timedelta(days=retention_days)))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("series", "samples", "hourly_totals", "gap_spans", "events", "epochs")
    }
    raw_older = conn.execute(
        "SELECT COUNT(*) FROM samples WHERE observed_at < ?",
        (to_iso(end - timedelta(days=retention_days)),),
    ).fetchone()[0]
    conn.close()
    _chmod_appdata(db.parent)
    fixture.stop()
    lock.release()

    duration = time.monotonic() - started
    manifest = {
        "schema": 1,
        "kind": "jarvis-qualification-seed",
        "generated_at": to_iso(end),
        "seed": seed,
        "source_id": source_id,
        "base_url": actual_url,
        "database": str(db),
        "uptime_origin": origin,
        "window": {
            "start": to_iso(window_start),
            "end": to_iso(end),
            "window_days": window_days,
            "cadence_early_seconds": cadence_early,
            "cadence_recent_seconds": cadence_recent,
            "retention_days": retention_days,
        },
        "models": [m["model_name"] for m in models],
        "final_counters": [
            {**m, "counters": {k: str(v) for k, v in zip(COUNTER_KEYS, c.values())}}
            for m, c in zip(models, traffic.counters)
        ],
        "lifetime": {key: str(value) for key, value in expectation.lifetime.items()},
        "per_model": [{**model, "counters": {key: str(value) for key, value in totals.items()}}
                      for model, totals in zip(models, expectation.per_model)],
        "daily": {day: {k: (None if v is None else str(v)) for k, v in counters.items()}
                  for day, counters in sorted(expectation.daily.items())},
        "hourly_cases": {
            hour: {key: {k: str(v) for k, v in counters.items()} for key, counters in blocks.items()}
            for hour, blocks in sorted(expectation.hours.items())
        },
        "gap_only_hours": expectation.gap_hours,
        "epoch_cases": expectation.epoch_cases,
        "cases": [
            {"key": case.key, "start": to_iso(case.start), "end": to_iso(case.end), "note": case.note}
            for case in cases
        ],
        "polls": {"simulated": polls, "applied": applied, "failed": failed},
        "tables": counts,
        "raw_samples_older_than_retention": raw_older,
        "integrity_check": integrity,
        "purged_rows": removed,
        "duration_seconds": round(duration, 2),
    }
    receipts.dump("manifest.json", manifest)
    if not quiet:
        qual.receipt("seed duration s", round(duration, 1))
        qual.receipt("seed polls", f"{polls} (applied={applied}, failed={failed})")
        for table, count in counts.items():
            qual.receipt(f"seed rows {table}", count)
        qual.receipt("seed integrity_check", integrity)
        qual.receipt("seed raw samples older than retention", raw_older)
        qual.receipt("seed lifetime processed", _processed(expectation.lifetime))
    return manifest


def _new_expectation(models: list[dict[str, str]], start: datetime, end: datetime) -> Expectation:
    """Zero-filled expected totals for every UTC day in the simulated window."""
    days: dict[str, dict[str, int | None]] = {}
    day = start.date()
    while day <= end.date():
        days[day.isoformat()] = {key: 0 for key in COUNTER_KEYS}
        day += timedelta(days=1)
    return Expectation(
        lifetime={key: 0 for key in COUNTER_KEYS},
        per_model=[{key: 0 for key in COUNTER_KEYS} for _ in models],
        daily=days,
        hours={},
        gap_hours={},
        epoch_cases={},
    )


def _credit(expectation: Expectation, traffic: TrafficModel, last_success: list[dict[str, int]],
            models: list[dict[str, str]], moment: datetime, reset_index: int | None, *,
            recovered: bool, restart_poll: bool = False, request_absent: bool = False) -> None:
    """Fold one successful poll's credited deltas into the expected totals.

    A series' first observation is an opening balance and is never credited; a
    primary reset opens an epoch and credits nothing for the primary pair (the
    optional baselines survive a reset), and disjoint engine-0 uptime evidence
    opens an epoch that credits nothing at all. Deltas are attributed to the
    bucket of the poll that observed them, which is how the writer's approximate
    time attribution behaves (D5).
    """
    day = moment.date().isoformat()
    hour = f"{moment:%Y-%m-%dT%H}Z"  # domain.hour_label form
    if recovered:
        expectation.gap_hours[hour] = "recovered_after_outage"
    if restart_poll:
        expectation.epoch_cases.setdefault(hour, {})["restart_evidence"] = models[STATS_MODEL_INDEX]["model_name"]
    for index, model in enumerate(models):
        for key in COUNTER_KEYS:
            if request_absent and key in (REQUEST_INPUT, REQUEST_OUTPUT):
                # Unobserved: the writer keeps its baseline and opens an optional
                # gap, so the credit lands on the first poll that sees them again.
                continue
            current = traffic.counters[index][key]
            previous = last_success[index].get(key)
            last_success[index][key] = current
            if previous is None:
                continue  # opening balance
            delta = current - previous
            if restart_poll and index == STATS_MODEL_INDEX:
                continue  # new epoch: the whole poll is an opening balance
            if reset_index == index and key in (COMPUTED_INPUT, GENERATED_OUTPUT):
                expectation.epoch_cases.setdefault(hour, {})["counter_reset"] = model["model_name"]
                continue
            if delta <= 0:
                continue
            expectation.lifetime[key] += delta
            expectation.per_model[index][key] += delta
            if expectation.daily[day][key] is not None:
                expectation.daily[day][key] += delta
            block = expectation.hours.setdefault(hour, {}).setdefault(key, {})
            name = model["model_name"]
            block[name] = block.get(name, 0) + delta


def _processed(lifetime: dict[str, int]) -> str:
    return str(lifetime[COMPUTED_INPUT] + lifetime[GENERATED_OUTPUT])


def _split_base_url(base_url: str) -> tuple[str, int]:
    rest = base_url.split("://", 1)[1]
    host, _, port = rest.partition(":")
    return host, int(port or "80")


def _chmod_appdata(appdata: Path) -> None:
    for path in appdata.iterdir():
        if path.is_file():
            os.chmod(path, 0o660)


# ---------------------------------------------------------------------------
# Step 4 verification: the real `serve` against the manifest
# ---------------------------------------------------------------------------
def _get(url: str, timeout: int = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def verify(db: Path, manifest: dict, port: int | None = None) -> int:
    verifier = qual.Verifier()
    source_id = manifest["source_id"]
    end = datetime.fromisoformat(manifest["window"]["end"].replace("Z", "+00:00"))
    retention_days = manifest["window"]["retention_days"]

    host, _ = _split_base_url(manifest["base_url"])
    host = "127.0.0.1"  # the verifier is a host-side reader, not the container
    fixture = FixtureServer(model="qual-model", host=host).start()
    for entry in manifest["final_counters"]:
        labels = {k: v for k, v in entry.items() if k != "counters"}
        counters = entry["counters"]
        install_fixture(fixture.state, labels, counters[COMPUTED_INPUT], counters[GENERATED_OUTPUT],
                        counters[REQUEST_INPUT], counters[REQUEST_OUTPUT])
    fixture.state.uptime_origin = manifest["uptime_origin"]

    serve_port = port or qual.free_port()
    env = dict(os.environ)
    env.update({
        "DATABASE_PATH": str(db),
        "RADIANCE_BASE_URL": fixture.base_url,
        "SOURCE_ID": source_id,
        "POLL_INTERVAL_SECONDS": "15",
        "RAW_RETENTION_DAYS": str(retention_days),
        "PORT": str(serve_port),
        "DASHBOARD_BIND_ADDRESS": "127.0.0.1",
        "LOG_LEVEL": "WARNING",
    })
    server = subprocess.Popen(
        [sys.executable, "-c", "from jarvis_dashboard.cli import main; raise SystemExit(main(['serve']))"],
        cwd=ROOT / "src", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        ready = _wait_ready(serve_port)
        verifier.check("serve /health/ready", ready, True)
        if not ready:
            return verifier.exit_code()
        base = f"http://127.0.0.1:{serve_port}/api/v1"

        series: dict[str, str] = {}
        cursor = None
        while True:
            url = f"{base}/series?limit=100" + (f"&cursor={cursor}" if cursor else "")
            page = _get(url)
            for item in page["items"]:
                series[item["model_name"]] = item["series_id"]
            cursor = page.get("next_cursor")
            if not cursor:
                break
        verifier.check("series count", len(series), len(manifest["models"]))

        lifetime = {key: 0 for key in COUNTER_KEYS}
        for index, name in enumerate(manifest["models"]):
            summary = _get(f"{base}/summary?series_id={series[name]}")
            for key in COUNTER_KEYS:
                actual = summary["ranges"]["lifetime"]["counters"][key]["tokens"]
                verifier.check(f"lifetime {name} {key}", actual, manifest["per_model"][index]["counters"][key])
                lifetime[key] += int(actual or 0)
        for key in COUNTER_KEYS:
            verifier.check(f"lifetime total {key}", str(lifetime[key]), manifest["lifetime"][key])

        # queries.MAX_BUCKETS["day"] = 366: the widest legal daily window.
        start_day = to_iso((end - timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0))
        end_day = to_iso((end + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        daily: dict[str, dict[str, int | None]] = {}
        for name in manifest["models"]:
            history = _get(f"{base}/history?series_id={series[name]}"
                           f"&from={start_day}&to={end_day}&bucket=day")
            for bucket in history["buckets"]:
                day = bucket["bucket"][:10]
                row = daily.setdefault(day, {key: 0 for key in COUNTER_KEYS})
                for key in COUNTER_KEYS:
                    tokens = bucket["counters"][key]["tokens"]
                    if tokens is None:
                        if bucket["counters"][key]["observed_intervals"] == 0:
                            row[key] = None if row[key] == 0 else row[key]
                    elif row[key] is not None:
                        row[key] += int(tokens)
        checked = 0
        for day, expected in sorted(manifest["daily"].items()):
            if day < start_day[:10]:
                continue
            actual = daily.get(day)
            for key in COUNTER_KEYS:
                want = expected[key]
                got = None if actual is None else actual[key]
                got_s = None if got is None else str(got)
                verifier.check(f"daily {day} {key}", got_s, want)
                checked += 1
        verifier.receipt("daily days checked", checked)
        idle = next(c["start"][:10] for c in manifest["cases"] if c["key"] == "idle_day")
        down = next(c["start"][:10] for c in manifest["cases"] if c["key"] == "collector_downtime")
        verifier.check("idle day is valid zero", daily.get(idle, {}).get(COMPUTED_INPUT), 0)
        verifier.check("downtime day is null", daily.get(down, {}).get(COMPUTED_INPUT), None)

        midnight = end.replace(hour=0, minute=0, second=0, microsecond=0)
        model0 = manifest["models"][0]
        hours = _get(f"{base}/history?series_id={series[model0]}"
                     f"&from={to_iso(midnight - timedelta(days=2))}&to={to_iso(midnight + timedelta(days=1))}"
                     f"&bucket=hour")
        by_hour = {bucket["bucket"]: bucket for bucket in hours["buckets"]}
        gap_hour = f"{midnight - timedelta(hours=1):%Y-%m-%dT%H}Z"
        recovered_hour = f"{midnight:%Y-%m-%dT%H}Z"
        gap_bucket = by_hour.get(gap_hour)
        verifier.check("gap-only hour is null",
                       None if gap_bucket is None else gap_bucket["counters"][COMPUTED_INPUT]["tokens"], None)
        recovered = by_hour.get(recovered_hour)
        flags = recovered["counters"][COMPUTED_INPUT]["quality_flags"] if recovered else []
        verifier.check("recovered hour flags",
                       [f for f in ("collection_gap", "time_attribution_approximate") if f in flags],
                       ["collection_gap", "time_attribution_approximate"])
        for key in COUNTER_KEYS:
            verifier.check(f"recovered hour {key}",
                           None if recovered is None else recovered["counters"][key]["tokens"],
                           _expected_hour_tokens(manifest, recovered_hour, key, model0, observed=True))
        previous_hour = f"{midnight - timedelta(days=1):%Y-%m-%dT%H}Z"
        restart_bucket = by_hour.get(previous_hour)
        # The restart-evidence poll credits nothing for the stats model; only the
        # post-restart polls of that hour carry tokens. Crediting the outage span
        # across the restart boundary would overshoot these values.
        for key in COUNTER_KEYS:
            verifier.check(f"previous-midnight hour {key}",
                           None if restart_bucket is None else restart_bucket["counters"][key]["tokens"],
                           _expected_hour_tokens(manifest, previous_hour, key, model0, observed=True))
        epochs: list[str] = []
        cursor = None
        for name in manifest["models"]:
            while True:
                summary = _get(f"{base}/summary?series_id={series[name]}&epoch_limit=20"
                               + (f"&epoch_cursor={cursor}" if cursor else ""))
                epochs.extend(e["reason"] for e in summary["epochs"]["items"])
                cursor = summary["epochs"].get("next_cursor")
                if not cursor:
                    break
        verifier.check("restart_evidence epoch present", "restart_evidence" in epochs, True)
        verifier.check("counter_reset epoch present", "counter_reset" in epochs, True)

        conn = storage.open_storage(db)
        try:
            cutoff = to_iso(end - timedelta(days=retention_days))
            verifier.check("raw samples older than retention",
                           conn.execute("SELECT COUNT(*) FROM samples WHERE observed_at < ?",
                                        (cutoff,)).fetchone()[0], 0)
            verifier.check("integrity_check", conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
    finally:
        server.terminate()
        server.wait(timeout=30)
        fixture.stop()
    return verifier.exit_code()


def _expected_hour_tokens(manifest: dict, hour: str, key: str, model: str, *, observed: bool) -> str | None:
    """Manifest expectation for one counter of one model in one hour.

    `observed` distinguishes an hour that was polled with no credited tokens
    (a valid "0") from an hour with no observation at all (null).
    """
    value = manifest["hourly_cases"].get(hour, {}).get(key, {}).get(model)
    if value is None:
        return "0" if observed else None
    return str(value)


def _wait_ready(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic one-year history seeder (D5)")
    parser.add_argument("--db", type=Path, required=True, help="appdata database path")
    parser.add_argument("--base-url", default="http://127.0.0.1:0",
                        help="fixture origin; must equal the container's RADIANCE_BASE_URL")
    parser.add_argument("--source-id", default=qual.SOURCE_ID)
    parser.add_argument("--seed", type=int, default=20260426)
    parser.add_argument("--end", default=None, help="simulated end (default: now, UTC)")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest output path")
    parser.add_argument("--window-days", type=int, default=367, help="D5 window (test runs may shorten)")
    parser.add_argument("--cadence-early", type=int, default=300)
    parser.add_argument("--cadence-recent", type=int, default=15)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RAW_RETENTION_DAYS)
    parser.add_argument("--case-days", default="",
                        help="designed-case placement, e.g. idle_day=2,collector_downtime=9 "
                             "(short test runs only; default: the D5 offsets)")
    parser.add_argument("--verify", action="store_true", help="compare the real serve API to the manifest")
    args = parser.parse_args()
    case_days = dict(pair.split("=", 1) for pair in args.case_days.split(",") if pair)
    case_days = {key: int(value) for key, value in case_days.items()}

    end = (datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end
           else datetime.now(timezone.utc).replace(microsecond=0))
    if args.verify:
        manifest = json.loads((args.manifest or Path(f"{args.db}.manifest.json")).read_text())
        return verify(args.db, manifest)

    run_dir = args.manifest.parent if args.manifest else args.db.parent
    receipts = qual.Receipts(run_dir)
    manifest = seed_history(
        args.db, args.base_url, args.source_id, args.seed,
        end=end, window_days=args.window_days, cadence_early=args.cadence_early,
        cadence_recent=args.cadence_recent, retention_days=args.retention_days, receipts=receipts,
        case_days=case_days,
    )
    target = args.manifest or args.db.parent / "seed-manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    qual.receipt("manifest", str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
