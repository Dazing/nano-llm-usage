#!/usr/bin/env python3
"""Delivery 4 resource workload (plan D6/D7/D9, Step 7): one year of history,
one container, two dashboard tabs, a 366-day query loop and a live backup.

Hard requirements are failures (exit non-zero). Preferred-target misses print a
distinct non-failing `TARGET MISS` line with the exact value. Nothing here runs
when plan D1's prerequisites are missing: the measurement is defined against
the image under the real cgroup limits, and a host process cannot answer it.

  .venv/bin/python scripts/qualify_resources.py --duration-minutes 30
  .venv/bin/python scripts/qualify_resources.py --preflight-only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
for _extra in (ROOT / "scripts", ROOT / "tests" / "fixtures", ROOT / "src"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import qualify_common as qc  # noqa: E402
import smoke_frontend as sf  # noqa: E402
from browser_driver import Browser  # noqa: E402
from jarvis_dashboard.domain import COUNTER_KEYS, PRIMARY_KEYS  # noqa: E402
from qualify_common import (  # noqa: E402
    SOURCE_ID,
    Compose,
    FixtureControl,
    OomWatcher,
    RunLog,
    Sampler,
    fresh_appdata,
    qualification_env,
    qualification_network,
    qualification_root,
    sh,
)
from qualify_image import api, container_pid, documented, first_series  # noqa: E402

POLL_SECONDS = 15                  # plan Step 7.1: the workload runs the production cadence
WARMUP_SECONDS = 300               # plan Step 7.6: five warm-up minutes before measuring
HISTORY_EVERY = 30                 # plan D6: one 366-day query every 30 s
TAB_EVERY = 60                     # plan D6: per-tab probe every 60 s
MEMORY_MAX_BYTES = 268435456       # the limit the measurement is defined against
CPU_MAX_EXPECTED = "50000 100000"  # == qc.CPU_MAX, read from the container's cgroup
MIB = 1024 * 1024
NON_RECLAIMABLE_BAR = 192 * MIB    # D7 hard: 75 % of the 256 MiB limit
RSS_TARGET = 100 * MIB             # D7 target: median steady VmRSS
LATENCY_TARGET = 1.0               # D7 target: p95 summary and 366-day history, seconds
BACKUP_AT = 15 * 60                # plan D6: the online backup at measured minute 15
PER_POLL_TRAFFIC = {"computed_input": 4000, "generated_output": 600,
                    "request_input": 4500, "request_output": 700}


def readiness(port: int) -> int:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=10) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except (URLError, TimeoutError, OSError):
        return 0


def to_epoch(text: str) -> float:
    """Seconds since the epoch for the API's `%Y-%m-%dT%H:%M:%S.%fZ` stamps."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# live upstream: counters keep advancing from the manifest's final values (D9)
# ---------------------------------------------------------------------------
class GrowingFixture:
    """The fixture the container polls: manifest values plus continuous growth."""

    def __init__(self, control: FixtureControl, manifest: dict) -> None:
        self.control = control
        self.rows = [dict(row) for row in manifest["final_counters"]]
        self.published = {key: 0 for key in COUNTER_KEYS}
        self.per_poll = {key: PER_POLL_TRAFFIC[key] * len(self.rows) for key in COUNTER_KEYS}
        control.load_manifest(manifest)
        control.set_counters(self.rows)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self, interval: float) -> None:
        self._interval = interval
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _run(self) -> None:
        rng = 12345
        while not self._stop.wait(self._interval):
            # Deterministic jitter so the deltas are not a flat line, matching
            # the seeded traffic shape the manifest describes.
            rng = (1103515245 * rng + 12345) % (2 ** 31)
            scale = 0.6 + 0.8 * (rng / 2 ** 31)
            step = {key: max(1, int(value * scale)) for key, value in PER_POLL_TRAFFIC.items()}
            self.rows = self.control.advance(self.rows, step)
            for key in COUNTER_KEYS:
                self.published[key] += step[key] * len(self.rows)

    @property
    def requests(self) -> int:
        return self.control.state.requests


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------
def tab_probe(log: RunLog, browser: Browser, page, url: str) -> dict:
    """One per-tab sample: timers, canvases, JS heap, summary timings (plan D6)."""
    timers = sf.probe(page, "p.timers.filter((t) => !p.cleared.includes(t.id)).map((t) => t.ms)")
    canvases = sf.count_of(page, "canvas")
    heap: dict[str, float] = {}
    try:
        browser.command("Performance.enable", {}, session=page.session)
        metrics = browser.command("Performance.getMetrics", {}, session=page.session)
        heap = {row["name"]: row["value"] for row in metrics.get("metrics", [])
                if row["name"] in ("JSHeapUsedSize", "JSHeapTotalSize", "Nodes", "Documents")}
    except Exception as exc:  # noqa: BLE001 - a missing metric is a report line, not a gate
        heap = {"error": str(exc)}
    timings = page.evaluate(
        "(() => { const out = [];"
        " for (const e of performance.getEntriesByType('resource')) {"
        "   if (!e.name.includes('/api/v1/')) continue;"
        "   out.push({path: e.name.split('/api/v1')[1].split('?')[0],"
        "             duration: e.duration, status: e.responseStatus ?? null}); }"
        " performance.clearResourceTimings(); return out; })()"
    )
    record = {"t": round(time.time(), 3), "url": url, "timers": timers,
              "canvases": canvases, "heap_bytes": heap.get("JSHeapUsedSize"),
              "heap_total_bytes": heap.get("JSHeapTotalSize"), "nodes": heap.get("Nodes"),
              "timings": timings}
    log.jsonl("tabs.jsonl", record)
    return record


def history_loop(log: RunLog, port: int, series_id: str, stop: threading.Event,
                 cadence: int = HISTORY_EVERY) -> None:
    """One aligned 366-day daily query every 30 s, one request at a time (D6)."""
    while not stop.is_set():
        end = qc.utc_now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=365)
        query = (f"series_id={series_id}&bucket=day"
                 f"&from={qc.day_label(start)}T00:00:00Z&to={qc.day_label(end)}T00:00:00Z")
        began = time.monotonic()
        status, body = api(port, "/history", query)
        seconds = time.monotonic() - began
        log.jsonl("latency.jsonl", {
            "t": round(time.time(), 3), "path": "/api/v1/history?bucket=day",
            "seconds": round(seconds, 4), "status": status,
            "bucket_count": body.get("bucket_count"),
        })
        stop.wait(cadence)


def chromium_rss(pid: int) -> int:
    """Total RSS of the browser process tree, reported separately (plan D7)."""
    out = sh(["ps", "-o", "rss=", "--ppid", str(pid)], timeout=60).stdout
    total = 0
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            total += int(line)
    return total * 1024


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


# ---------------------------------------------------------------------------
# AC7 growth extrapolation
# ---------------------------------------------------------------------------
def growth_report(db: Path, receipts: RunLog, *, window_days: float, series: int) -> dict:
    """Per-table bytes on a `VACUUM INTO` scratch copy, then the projection."""
    scratch = db.parent / "growth-scratch.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("VACUUM INTO ?", (str(scratch),))
    finally:
        conn.close()
    report: dict[str, object] = {"scratch_bytes": scratch.stat().st_size if scratch.exists() else 0}
    conn = sqlite3.connect(scratch)
    try:
        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("samples", "hourly_totals", "gap_spans", "events", "series")}
        report["row_counts"] = counts
        per_table: dict[str, int] = {}
        try:
            conn.execute("ANALYZE")
            rows = conn.execute(
                "SELECT m.name, SUM(s.pgsize) FROM dbstat s"
                " JOIN sqlite_master m ON m.name = s.name GROUP BY m.name").fetchall()
            per_table = {str(name): int(size) for name, size in rows}
            report["per_table_source"] = "dbstat"
        except sqlite3.Error as exc:  # dbstat is a compile-time option
            report["per_table_source"] = f"dbstat unavailable ({exc}); delete-rows + vacuum diff"
            for name in ("samples", "hourly_totals", "gap_spans", "events"):
                probe = scratch.parent / f"probe-{name}.sqlite3"
                conn.execute("VACUUM INTO ?", (str(probe),))
                probe_conn = sqlite3.connect(probe)
                probe_conn.execute(f"DELETE FROM {name}")
                probe_conn.execute("VACUUM")
                after = probe.stat().st_size
                probe_conn.close()
                probe.unlink(missing_ok=True)
                per_table[name] = report["scratch_bytes"] - after
        report["per_table_bytes"] = per_table

        def with_indexes(table: str) -> int:
            """A row's real cost: its table plus every index that points at it."""
            names = [table]
            try:
                names += [str(row[1]) for row in conn.execute(f"PRAGMA index_list({table})")]
            except sqlite3.Error:
                pass
            return sum(per_table.get(name, 0) for name in names)

        samples = counts["samples"]
        hourly = counts["hourly_totals"]
        report["bytes_per_sample"] = (round(with_indexes("samples") / samples, 2)
                                      if samples else None)
        report["bytes_per_hourly_row"] = (round(with_indexes("hourly_totals") / hourly, 2)
                                          if hourly else None)
        report["index_bytes"] = {"samples": with_indexes("samples"),
                                 "hourly_totals": with_indexes("hourly_totals")}
        # Assumptions, stated plainly (plan AC7): raw samples are kept for
        # RAW_RETENTION_DAYS (30), so the raw volume is bounded by 30 days of
        # polls; the unbounded term is the hourly rollup, one row per counter
        # per hour per series, plus the daily rollup the read path derives.
        hourly_rows_per_day = 24 * len(COUNTER_KEYS) * series
        report["assumptions"] = {
            "retention_days": 30,
            "poll_seconds": POLL_SECONDS,
            "series_count": series,
            "counters_per_hourly_row": 1,
            "row_bytes_include_indexes": True,
            "expected_hourly_rows_per_day": hourly_rows_per_day,
            "observed_hourly_rows_per_day": round(hourly / max(1.0, window_days), 1),
            "note": ("raw samples stay bounded by the retention window; hourly rollups grow"
                     " linearly with wall time and are the extrapolated term"),
        }
        per_day = (report["bytes_per_hourly_row"] or 0) * hourly_rows_per_day
        report["extrapolation"] = {
            "hourly_bytes_per_day": round(per_day, 1),
            "raw_bytes_retained": round(samples * (report["bytes_per_sample"] or 0)),
            "projected_1_year_bytes": round(per_day * 365),
            "projected_5_years_bytes": round(per_day * 365 * 5),
            "projected_10_years_bytes": round(per_day * 365 * 10),
        }
    finally:
        conn.close()
        scratch.unlink(missing_ok=True)
    receipts.dump("growth.json", report)
    return report


# ---------------------------------------------------------------------------
# verdicts (plan D7)
# ---------------------------------------------------------------------------
def verdicts(log: RunLog, samples: list[dict], tabs: list[dict], latencies: list[dict],
             chromium: dict, measured_from: float, poll: int,
             committed: dict, logs: str) -> dict:
    """Hard requirements fail the run; preferred targets print TARGET MISS."""
    window = [row for row in samples if row["t"] >= measured_from]
    rss = [row["VmRSS"] for row in window if row.get("VmRSS")]
    non_reclaimable = [row["non_reclaimable_bytes"] for row in window
                       if row.get("non_reclaimable_bytes") is not None]
    memory_peak = max((row.get("memory_peak") or 0) for row in window) if window else 0
    events: dict[str, int] = {}
    for row in window:
        for key, value in (row.get("memory_events") or {}).items():
            events[key] = max(events.get(key, 0), int(value))
    deltas: list[float] = []
    previous: float | None = None
    throttled = {"nr_throttled": 0, "throttled_usec": 0}
    for row in window:
        stat = row.get("cpu_stat") or {}
        used = stat.get("usage_usec")
        if used is not None and previous is not None:
            deltas.append(max(0.0, used - previous))
        if stat:
            throttled["nr_throttled"] = max(throttled["nr_throttled"], int(stat.get("nr_throttled", 0)))
            throttled["throttled_usec"] = max(throttled["throttled_usec"],
                                              int(stat.get("throttled_usec", 0)))
        previous = used
    # 5 s sampling windows (plan D6): usec per window / 1e6 / 5 = cores in use
    cpu_cores = [delta / 1_000_000 / 5.0 for delta in deltas]

    summary_ms = [entry["duration"] for tab in tabs for entry in tab["timings"]
                  if entry["path"] == "/api/v1/summary"]
    summary_s = [value / 1000.0 for value in summary_ms]
    history_s = [row["seconds"] for row in latencies if row["status"] == 200]
    warm = [row for row in window if row["t"] < measured_from + 1200]
    tail = [row for row in window if row["t"] >= measured_from + 600]

    def maximum(rows: list[dict], key: str) -> int | None:
        values = [row.get(key) for row in rows if row.get(key) is not None]
        return max(values) if values else None

    def peak_anon() -> int | None:
        values = [(row.get("memory_stat") or {}).get("anon") for row in window]
        values = [value for value in values if value is not None]
        return max(values) if values else None

    def anon_slope() -> float | None:
        points = [(row["t"], (row.get("memory_stat") or {}).get("anon")) for row in window]
        points = [(t, anon) for t, anon in points if anon is not None]
        if len(points) < 4:
            return None
        span = points[-1][0] - points[0][0]
        return round((points[-1][1] - points[0][1]) / span * 3600, 1) if span else None

    verdict: dict[str, object] = {
        "measured_seconds": round(window[-1]["t"] - window[0]["t"], 1) if len(window) > 1 else 0,
        "samples": len(window),
        "rss_median_bytes": statistics.median(rss) if rss else None,
        "rss_max_bytes": max(rss) if rss else None,
        "memory_peak_bytes": memory_peak,
        "memory_events_max": events,
        "non_reclaimable_max_bytes": max(non_reclaimable) if non_reclaimable else None,
        "memory_anon_peak_bytes": peak_anon(),
        "memory_anon_slope_bytes_per_hour": anon_slope(),
        "cpu_cores_avg": round(statistics.mean(cpu_cores), 4) if cpu_cores else None,
        "cpu_cores_p95": round(percentile(cpu_cores, 0.95) or 0, 4) if cpu_cores else None,
        "cpu_cores_max": round(max(cpu_cores), 4) if cpu_cores else None,
        "cpu_throttling": throttled,
        "threads_max": maximum(window, "Threads"),
        "fds_max": maximum(window, "fds"),
        "sockets_max": maximum(window, "tcp_established"),
        "pids_max": maximum(window, "pids_current"),
        "tail_threads_max": maximum(tail, "Threads"),
        "tail_fds_max": maximum(tail, "fds"),
        "tail_sockets_max": maximum(tail, "tcp_established"),
        "warm_threads_max": maximum(warm, "Threads"),
        "warm_fds_max": maximum(warm, "fds"),
        "warm_sockets_max": maximum(warm, "tcp_established"),
        "timers_per_tab": sorted({len(tab["timers"]) for tab in tabs}),
        "canvases_per_tab": sorted({tab["canvases"] for tab in tabs}),
        "chromium_rss_bytes": chromium.get("rss"),
        "chromium_heap_bytes": chromium.get("heap"),
        "summary_latency": {"n": len(summary_s), "p50": percentile(summary_s, 0.50),
                            "p95": percentile(summary_s, 0.95),
                            "max": max(summary_s) if summary_s else None},
        "history_latency": {"n": len(history_s), "p50": percentile(history_s, 0.50),
                            "p95": percentile(history_s, 0.95),
                            "max": max(history_s) if history_s else None},
        "db_sizes_max_bytes": committed.get("db_sizes"),
        "commit_continuity": committed,
        "skipped_poll_log_lines": logs.count("missed poll slot"),
        "five_xx_log_lines": sum(logs.count(code) for code in
                                 ("read_capacity_exceeded", "storage_unavailable",
                                  "read_saturated", "internal_error")),
    }

    # -- hard requirements (plan D7) ---------------------------------------
    log.check("no OOM in memory.events", events.get("oom", 0) + events.get("oom_kill", 0), 0)
    log.check("no Docker OOM event", committed.get("oom_events"), [])
    log.check("container never OOMKilled", committed.get("oom_killed"), False)
    log.check("no container restart", committed.get("restart_count"), 0)
    log.check("health never left healthy", committed.get("unhealthy_samples"), 0)
    log.check("non-reclaimable memory under the 192 MiB bar",
              (verdict["non_reclaimable_max_bytes"] or 0) <= NON_RECLAIMABLE_BAR, True)
    log.check("memory.peak reported exactly", isinstance(memory_peak, int) and memory_peak > 0, True)
    for key, label in (("Threads", "threads"), ("fds", "fds"), ("tcp_established", "sockets")):
        log.check(f"no {label} accumulation in the last 10 minutes",
                  (maximum(tail, key) or 0) <= (maximum(warm, key) or 0), True)
    log.check("one live poll timer per tab", max(verdict["timers_per_tab"] or [0]), 1)
    log.check("one canvas per tab", max(verdict["canvases_per_tab"] or [0]), 1)
    log.check("no committed-sample gap over 2 poll intervals",
              (committed.get("max_gap_seconds") or 0) <= 2 * poll, True)
    log.check("no skipped poll-slot log lines", verdict["skipped_poll_log_lines"], 0)
    log.check("committed samples match fixture requests",
              abs(int(committed.get("committed_samples", 0))
                  - int(committed.get("fixture_requests", 0))) <= 1, True)
    log.check("no 5xx responses during the workload", verdict["five_xx_log_lines"], 0)
    log.check("backup exit code", committed.get("backup_exit_code"), 0)
    log.check("backup integrity_check", committed.get("backup_integrity"), "ok")
    log.check("backup ledger identity holds", committed.get("backup_ledger_identity"), True)
    log.check("final ledger identity holds", committed.get("ledger_identity"), True)
    log.check("every tab kept polling at the cadence", committed.get("paused_tabs"), [])

    # -- preferred targets (non-failing, plan D7) --------------------------
    median_rss = verdict["rss_median_bytes"]
    if median_rss is None or median_rss >= RSS_TARGET:
        log.receipt("target_miss_median_rss", median_rss)
        print(f"TARGET MISS median steady VmRSS < 100 MiB: "
              f"{(median_rss or 0) / MIB:.1f} MiB over {len(rss)} samples")
    for label, values in (("summary", summary_s), ("366-day-history", history_s)):
        p95 = percentile(values, 0.95)
        if p95 is None or p95 >= LATENCY_TARGET:
            log.receipt(f"target_miss_p95_{label}", p95)
            print(f"TARGET MISS p95 {label} < 1 s: {p95} (n={len(values)})")
    log.dump("verdicts.json", verdict)
    return verdict


def commit_continuity(appdata: Path, manifest: dict, growing: GrowingFixture,
                     tabs: list[dict], poll: int, backup: dict) -> dict:
    """Plan D7 commit continuity + D6 ledger identity, read read-only."""
    db = appdata / "dashboard.sqlite3"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        stamps = [row[0] for row in conn.execute(
            "SELECT observed_at FROM samples ORDER BY observed_at")]
        gaps = [(to_epoch(b) - to_epoch(a)) for a, b in zip(stamps, stamps[1:])]
        committed_samples = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        series = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        epochs = conn.execute("SELECT COUNT(*) FROM epochs").fetchone()[0]
        lifetime: dict[str, int] = {}
        for key in PRIMARY_KEYS:
            total = conn.execute(
                "SELECT SUM(tokens) FROM hourly_totals t JOIN series s ON s.identity = t.identity"
                " WHERE t.source_id = ? AND t.counter_key = ?", (SOURCE_ID, key)).fetchone()[0]
            lifetime[key] = int(total or 0)
    finally:
        conn.close()
    result: dict[str, object] = {
        "committed_samples": committed_samples,
        "fixture_requests": growing.requests,
        "max_gap_seconds": round(max(gaps), 3) if gaps else None,
        "gaps_over_two_polls": [round(value, 1) for value in gaps if value > 2 * poll][:10],
        "series_count": series,
        "epoch_count": epochs,
    }
    # Ledger identity (D6): with no new epoch, lifetime = manifest lifetime + the
    # increments the harness published since the manifest was written.
    identity: dict[str, bool] = {}
    detail: dict[str, int] = {}
    for key in PRIMARY_KEYS:
        manifest_lifetime = sum(int(model["counters"][key]) for model in manifest["per_model"])
        expected = manifest_lifetime + growing.published[key]
        detail[f"{key}_expected"] = expected
        detail[f"{key}_actual"] = lifetime[key]
        # The increments published after the last poll are legitimately absent:
        # accept the last full poll's worth, and never accept a duplicate.
        identity[key] = expected - growing.per_poll[key] <= lifetime[key] <= expected
    result["ledger_identity"] = all(identity.values()) if identity else False
    result["ledger_detail"] = detail
    result["backup_ledger_identity"] = backup.get("ledger_identity")
    result["backup_exit_code"] = backup.get("backup_exit_code")
    result["backup_integrity"] = backup.get("backup_integrity")
    result["backup_path"] = backup.get("backup_path")
    result["paused_tabs"] = committed_paused_tabs(tabs, poll)
    return result


def committed_paused_tabs(tabs: list[dict], poll: int) -> list[dict]:
    """A tab whose summary fetches stopped is a failed run (plan Step 7.4)."""
    paused: list[dict] = []
    for url in sorted({tab["url"] for tab in tabs}):
        stamps = [tab["t"] for tab in tabs if tab["url"] == url
                  and any(entry["path"] == "/api/v1/summary" for entry in tab["timings"])]
        stamps.sort()
        for previous, current in zip(stamps, stamps[1:]):
            if current - previous > 3 * poll:
                paused.append({"url": url, "gap_seconds": round(current - previous, 1)})
                break
    return paused


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def newest_backup(appdata: Path) -> Path | None:
    folder = appdata / "backups"
    if not folder.is_dir():
        return None
    backups = sorted(folder.glob("*.sqlite3"))
    return backups[-1] if backups else None


def file_size_maxes(run_dir: Path) -> dict[str, int]:
    maxima: dict[str, int] = {}
    for row in read_jsonl(run_dir / "db-sizes.jsonl"):
        for key, value in row.items():
            if key == "t" or not isinstance(value, int):
                continue
            maxima[key] = max(maxima.get(key, 0), value)
    return maxima


def restart_count(run_dir: Path) -> int:
    started = {row.get("started_at") for row in read_jsonl(run_dir / "inspect.jsonl")
               if row.get("started_at")}
    return max(0, len(started) - 1)


def unhealthy_samples(run_dir: Path) -> int:
    return sum(1 for row in read_jsonl(run_dir / "inspect.jsonl")
               if row.get("health") not in ("healthy", None))


def oom_killed(run_dir: Path) -> bool:
    return any(row.get("oom_killed") for row in read_jsonl(run_dir / "inspect.jsonl"))


def snapshot_ledger(path: Path, manifest: dict, growing: GrowingFixture) -> tuple[bool, dict]:
    """Ledger identity for the online backup snapshot taken mid-workload."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        detail: dict[str, int] = {}
        identity = True
        for key in PRIMARY_KEYS:
            total = conn.execute(
                "SELECT SUM(tokens) FROM hourly_totals t JOIN series s ON s.identity = t.identity"
                " WHERE t.source_id = ? AND t.counter_key = ?", (SOURCE_ID, key)).fetchone()[0]
            manifest_lifetime = sum(int(model["counters"][key]) for model in manifest["per_model"])
            published = growing.published[key]
            lower = manifest_lifetime + published - 4 * growing.per_poll[key]
            detail[f"{key}_actual"] = int(total or 0)
            detail[f"{key}_max_expected"] = manifest_lifetime + published
            detail[f"{key}_min_expected"] = lower
            identity = identity and lower <= int(total or 0) <= manifest_lifetime + published
        return identity, detail
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# workload
# ---------------------------------------------------------------------------
def run_workload(log: RunLog, browser: Browser | None, minutes: int, seed_days: int,
                 seed_value: int, keep: bool) -> int:
    with qualification_root("d4-workload") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        port = qc.free_port()
        fixture_port = qc.free_port()
        fixture = FixtureControl(gateway, port=fixture_port).start()
        base_url = fixture.base_url

        print(f"--- seed: one year of history through the real collector ({seed_days} days)")
        manifest_path = tmp / "seed-manifest.json"
        seed = sh([sys.executable, str(ROOT / "scripts" / "seed_history.py"),
                   "--db", str(appdata / "dashboard.sqlite3"), "--base-url", base_url,
                   "--source-id", SOURCE_ID, "--seed", str(seed_value),
                   "--window-days", str(seed_days), "--cadence-recent", str(POLL_SECONDS),
                   "--manifest", str(manifest_path)], cwd=ROOT, timeout=7200)
        log.command("seed_history", ["scripts/seed_history.py", "…"], seed.returncode,
                    seed.stdout, seed.stderr)
        if seed.returncode != 0:
            log.require(False, f"seeding failed: {seed.stderr.strip()[:400]}")
            fixture.stop()
            print(log.summary())
            return log.exit_code()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        log.receipt("seed_polls", manifest["polls"])
        log.receipt("seed_tables", manifest["tables"])

        env = qualification_env(appdata, port, base_url, POLL_SECONDS)
        stack = Compose(project="jarvis-4w", appdata=appdata, env=env,
                        owned_roots=(appdata,), temporary_root=tmp)
        growing = GrowingFixture(fixture, manifest)
        sampler: Sampler | None = None
        oom: OomWatcher | None = None
        stop = threading.Event()
        history: threading.Thread | None = None
        chromium: dict[str, object] = {}
        backup: dict[str, object] = {}
        measured_from = 0.0
        pages: list = []
        try:
            print("--- compose up (production file + qualification override)")
            stack.up(timeout=300)
            log.check("readiness on a one-year volume",
                      qc.wait_until(lambda: readiness(port) == 200, timeout=300), True)
            series_id = first_series(port, manifest["models"][0]["model_name"])
            log.require(bool(series_id), "the seeded series is served")
            growing.start(POLL_SECONDS)

            pid = container_pid(stack)
            log.receipt("container_pid", pid)
            cgroup = qc.cgroup_dir(pid)
            log.receipt("cgroup_dir", str(cgroup))
            log.check("the measured container runs under the memory limit",
                      qc.read_cgroup(cgroup, "memory.max"), str(MEMORY_MAX_BYTES))
            log.check("the measured container runs under the cpu limit",
                      qc.read_cgroup(cgroup, "cpu.max"), CPU_MAX_EXPECTED)

            if browser is not None:
                for _ in range(2):  # plan Step 7.4: two independent windows
                    page = browser.new_page(*sf.DESKTOP)
                    page.add_script_on_new_document(sf.PROBE)
                    page.navigate(f"http://127.0.0.1:{port}/")
                    page.click('[data-testid="range-30d"]')
                    qc.wait_until(lambda page=page: sf.fetches(page, "/api/v1/summary"),
                                  timeout=60)
                    pages.append(page)

            oom = OomWatcher(stack.container_id() or "", log)
            oom.start()
            sampler = Sampler(pid, cgroup, log, stack.container_id, appdata=appdata)
            sampler.start()
            history = threading.Thread(target=history_loop,
                                       args=(log, port, series_id, stop), daemon=True)
            history.start()

            print(f"--- warm-up {WARMUP_SECONDS // 60} min, then {minutes} measured minutes")
            time.sleep(WARMUP_SECONDS)
            measured_from = time.time()
            log.receipt("measured_from", round(measured_from, 3))
            measured_until = measured_from + minutes * 60
            backup_done = False
            while time.time() < measured_until:
                time.sleep(min(TAB_EVERY, max(5.0, measured_until - time.time())))
                for index, page in enumerate(pages):
                    tab_probe(log, browser, page, f"tab-{index + 1}")
                if browser is not None:
                    chromium["rss"] = chromium_rss(browser.pid)
                    chromium["heap"] = max((tab.get("heap_bytes") or 0)
                                           for tab in read_jsonl(log.dir / "tabs.jsonl"))
                if not backup_done and time.time() - measured_from >= BACKUP_AT:
                    backup_done = True
                    print("--- the documented online backup at measured minute 15")
                    result = documented(log, stack, appdata, base_url, qc.BACKUP_COMMAND,
                                        "documented backup during the workload")
                    backup["backup_exit_code"] = result.returncode
                    snapshot = newest_backup(appdata)
                    backup["backup_path"] = str(snapshot) if snapshot else None
                    if snapshot:
                        conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
                        backup["backup_integrity"] = conn.execute(
                            "PRAGMA integrity_check").fetchone()[0]
                        conn.close()
                        backup["ledger_identity"], backup["ledger_detail"] = snapshot_ledger(
                            snapshot, manifest, growing)
            stop.set()
            if history:
                history.join(timeout=60)
            sampler.stop()
            oom_events = oom.stop()

            print("--- timed stop")
            began = time.monotonic()
            stopped = stack.stop()
            log.receipt("stop_seconds", round(time.monotonic() - began, 3))
            log.check("stop exit code", stopped.returncode, 0)
            row = stack.inspect()[0]
            log.check("State.ExitCode after the workload", (row.get("State") or {}).get("ExitCode"), 0)
            logs = stack.logs()
            log.write("container.log", logs)

            tabs = read_jsonl(log.dir / "tabs.jsonl")
            committed = commit_continuity(appdata, manifest, growing, tabs, POLL_SECONDS, backup)
            committed["db_sizes"] = file_size_maxes(log.dir)
            committed["restart_count"] = restart_count(log.dir)
            committed["unhealthy_samples"] = unhealthy_samples(log.dir)
            committed["oom_killed"] = oom_killed(log.dir)
            committed["oom_events"] = oom_events
            verdict = verdicts(log, read_jsonl(log.dir / "samples.jsonl"), tabs,
                               read_jsonl(log.dir / "latency.jsonl"), chromium, measured_from,
                               POLL_SECONDS, committed, logs)
            growth_report(appdata / "dashboard.sqlite3", log,
                          window_days=manifest["window"]["window_days"],
                          series=manifest["tables"].get("series", 1))
            log.receipt("verdict_summary", {
                "measured_seconds": verdict["measured_seconds"],
                "rss_median_mib": round((verdict["rss_median_bytes"] or 0) / MIB, 1),
                "rss_max_mib": round((verdict["rss_max_bytes"] or 0) / MIB, 1),
                "memory_peak_mib": round((verdict["memory_peak_bytes"] or 0) / MIB, 1),
                "non_reclaimable_max_mib": round((verdict["non_reclaimable_max_bytes"] or 0) / MIB, 1),
                "cpu_cores_p95": verdict["cpu_cores_p95"],
                "summary_p95_seconds": verdict["summary_latency"]["p95"],
                "history_p95_seconds": verdict["history_latency"]["p95"],
                "db_sizes_max_bytes": verdict["db_sizes_max_bytes"],
            })
        finally:
            stop.set()
            growing.stop()
            if sampler:
                sampler.stop()
            if oom:
                oom.stop()
            fixture.stop()
            if not keep:
                stack.down(volumes=True)
    return log.exit_code()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--duration-minutes", type=int, default=30,
                        help="measured minutes (plan minimum: 30)")
    parser.add_argument("--seed-days", type=int, default=367)
    parser.add_argument("--seed", type=int, default=20260426)
    parser.add_argument("--out", type=Path, help="receipt directory")
    parser.add_argument("--no-browser", action="store_true",
                        help="run without the two tabs (resource gates only; not a full pass)")
    parser.add_argument("--keep", action="store_true", help="leave the volume for inspection")
    parser.add_argument("--growth-only", action="store_true",
                        help="measure AC7 growth math on an existing volume (no Docker)")
    parser.add_argument("--db", type=Path, help="volume for --growth-only")
    parser.add_argument("--window-days", type=float, default=367.0,
                        help="seeded window represented by --db (for --growth-only)")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.duration_minutes < 30 and not args.preflight_only:
        parser.error("--duration-minutes must be at least 30 (plan D6/AC7)")

    out = args.out or ROOT / "docs" / "receipts" / "delivery-4" / qc.new_run_id()
    log = RunLog(out)
    log.receipt("run_id", out.name)
    log.receipt("duration_minutes", args.duration_minutes)
    env, missing = qc.preflight(require_browser=not args.no_browser, require_root=True,
                                appdata_hint=Path("/tmp"))
    log.dump("environment.json", env)
    if args.preflight_only:
        print(json.dumps(env, indent=2))
        for item in missing:
            print(f"  - {item}")
        return 1 if missing else 0
    if args.growth_only:
        # AC7 growth math is a host measurement: per-row bytes on a compacted
        # copy of an existing volume, no container involved.
        if args.db is None or not args.db.exists():
            parser.error("--growth-only requires --db pointing at an existing volume")
        probe = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        try:
            series = probe.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        finally:
            probe.close()
        report = growth_report(args.db, log, window_days=args.window_days, series=int(series))
        log.check("per_table_source", report["per_table_source"], "dbstat")
        log.require(bool(report["bytes_per_sample"]), "bytes per sample measured")
        log.require(bool(report["bytes_per_hourly_row"]), "bytes per hourly row measured")
        for key in ("scratch_bytes", "row_counts", "bytes_per_sample", "bytes_per_hourly_row",
                    "assumptions", "extrapolation"):
            log.receipt(key, report[key])
        print(log.summary())
        return log.exit_code()
    if missing:
        print("[FAIL] qualification prerequisites missing:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        log.receipt("missing_prerequisites", missing)
        log.require(False, "preflight refused: the workload requires a container runtime")
        print(log.summary())
        return log.exit_code()

    browser = None
    profile = None
    try:
        if not args.no_browser:
            profile = Path(tempfile.mkdtemp(prefix="jarvis-4w-chrome-"))
            browser = Browser.launch(profile, [])
        return run_workload(log, browser, args.duration_minutes, args.seed_days, args.seed,
                            args.keep)
    finally:
        if browser is not None:
            browser.close()
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)
        print(log.summary())


if __name__ == "__main__":
    raise SystemExit(main())
