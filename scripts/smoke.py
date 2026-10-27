#!/usr/bin/env python3
"""End-to-end smoke runner (plan §5 scenarios, numeric receipts).

Deliveries 1-2: collector accounting, ownership, transport limits, then the
real `serve` process, the bounded read API, online backup, and offline restore.

Runs the controllable upstream fixture (tests/fixtures), the real collector CLI
as subprocesses, and reads SQLite back read-only. Every check prints a numeric
receipt; the script exits non-zero on the first failed expectation.

Usage: .venv/bin/python scripts/smoke.py
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
sys.path.insert(0, str(ROOT / "src"))

from fixture_server import FixtureServer, install_fixture  # noqa: E402

from jarvis_dashboard import storage  # noqa: E402
from jarvis_dashboard.domain import (  # noqa: E402
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    REQUEST_INPUT,
)

L = {"model_name": "fixture-model", "engine": "0"}
CI, GO = "radiance:prefill_tokens_total", "radiance:decode_tokens_total"
RI, RO = "vllm:prompt_tokens_total", "vllm:generation_tokens_total"
POLL = 5
TWO_POLLS = 11  # > 2 * poll with slack
BUDGET_START = time.monotonic()


def receipt(name: str, value: object) -> None:
    print(f"[receipt] {name} = {value}", flush=True)


def check(name: str, actual: object, expected: object) -> None:
    ok = actual == expected
    print(f"[{'ok ' if ok else 'FAIL'}] {name}: {actual!r} (expected {expected!r})", flush=True)
    if not ok:
        raise AssertionError(f"{name}: {actual!r} != {expected!r}")
    if time.monotonic() - BUDGET_START > 900:  # delivery 2 adds serve/backup/restore
        raise AssertionError("smoke budget exceeded")


def hour_window(db: Path) -> tuple[str, str]:
    row = q(
        db,
        "SELECT MIN(hour) AS first_hour, MAX(hour) AS last_hour FROM hourly_totals "
        "WHERE counter_key=?",
        (COMPUTED_INPUT,),
    )[0]
    return row["first_hour"], row["last_hour"]


def next_hour(label: str) -> str:
    stamp = _dt.datetime.strptime(f"{label[:-1]}:00:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.UTC
    )
    return (stamp + _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_reasons(db: Path) -> list[str]:
    return [row["reason"] for row in q(db, "SELECT reason FROM epochs ORDER BY epoch_id DESC")]


def ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def q(db: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    if not db.exists():
        return []
    with ro(db) as conn:
        return conn.execute(sql, params).fetchall()


def total(db: Path, counter: str, source: str = "s1") -> int:
    if not db.exists():
        return 0
    rows = q(
        db,
        "SELECT COALESCE(SUM(tokens), 0) AS t FROM hourly_totals "
        "WHERE source_id=? AND counter_key=?",
        (source, counter),
    )
    return int(rows[0]["t"])


class UptimeTicker:
    """Keep the fixture uptime advancing in real time between snapshot edits.

    A flat uptime between polls makes the derived process-start interval slide
    and look like a restart; the collector must see a chainable clock.
    """

    def __init__(self, server: FixtureServer, base: float = 100.0):
        self._st = server.state
        self._start = time.monotonic()
        self._base = base
        self._stop = __import__("threading").Event()
        self._thread = __import__("threading").Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._st.uptime_s = self._base + (time.monotonic() - self._start)
            time.sleep(0.25)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def reset_state(server: FixtureServer) -> None:
    st = server.state
    st.samples.clear()
    st.uptime_s = 100.0
    st.stats_fail = False
    st.stats_body = None
    st.metrics_status = None
    st.metrics_body = None
    st.delay_s = 0.0
    st.chunk_delay_s = 0.0
    st.oversize = False
    st.redirect = False
    st.requests.clear()


def set_counts(
    server: FixtureServer, ci: int, go: int,
    ri: int | None = None, ro_: int | None = None,
    uptime_start: float | None = None, uptime_fixed: float | None = None,
) -> None:
    """Publish a new upstream snapshot with a chainable uptime reading."""
    st = server.state
    if uptime_fixed is not None:
        st.uptime_s = uptime_fixed
    elif uptime_start is not None:
        st.uptime_s = 100 + (time.monotonic() - uptime_start)
    st.drop_series(L)
    values: dict[str, object] = {CI: ci, GO: go}
    if ri is not None:
        values[RI] = ri
        values[RO] = ro_
    st.set_series(L, values)


LAUNCHED: list["CollectorProc"] = []


def stop_all() -> None:
    """Terminate anything still running after a failed scenario."""
    for proc in LAUNCHED:
        if proc.proc is not None and proc.proc.poll() is None:
            proc.proc.terminate()
            try:
                proc.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.proc.kill()


class CollectorProc:
    def __init__(self, db: Path, base_url: str):
        self.db = db
        self.env = {
            **os.environ,
            "RADIANCE_BASE_URL": base_url,
            "SOURCE_ID": "s1",
            "POLL_INTERVAL_SECONDS": str(POLL),
            "HTTP_TIMEOUT_SECONDS": "3",
            "DATABASE_PATH": str(db),
            "RAW_RETENTION_DAYS": "30",
            "LOG_LEVEL": "WARNING",
        }
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "jarvis_dashboard.cli", "collect"],
            cwd=ROOT,
            env=self.env,
            stderr=subprocess.PIPE,
            text=True,
        )
        LAUNCHED.append(self)

    def stop(self, sig=signal.SIGTERM, timeout: int = 30) -> tuple[int, float, str]:
        assert self.proc is not None
        started = time.monotonic()
        self.proc.send_signal(sig)
        rc = self.proc.wait(timeout=timeout)
        err = self.proc.stderr.read() if self.proc.stderr else ""
        return rc, time.monotonic() - started, err

    def run_to_exit(self, timeout: int = 20) -> tuple[int, str]:
        self.start()
        rc = self.proc.wait(timeout=timeout)
        return rc, self.proc.stderr.read()

    def kill_mid_poll(self) -> None:
        assert self.proc is not None
        time.sleep(2)  # let a poll get in flight
        self.proc.kill()  # SIGKILL: crash at the OS level
        self.proc.wait(timeout=10)


def wait_for(predicate, timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(1)
    return False


def has_series(db: Path) -> bool:
    try:
        return len(q(db, "SELECT 1 FROM series WHERE source_id='s1'")) > 0
    except sqlite3.OperationalError:
        return False  # database still being created


def volume_lock_held(db: Path) -> bool:
    """True once a live writer holds the exclusive flock on ``<db>.lock``.

    Series presence is not writer readiness: a restored volume already has
    series, so a scenario that expects a busy-target refusal must observe the
    lock itself (the same ``fcntl.flock`` ``storage.acquire_volume_lock`` uses).
    """
    lock_path = db.with_name(db.name + ".lock")
    if not lock_path.exists():
        return False
    with lock_path.open("a+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False


def scenario_normal_and_restart(server: FixtureServer, db: Path) -> None:
    print("--- scenario 1: normal deltas, restart independence, optionals separate")
    start = time.monotonic()
    ticker = UptimeTicker(server)
    set_counts(server, 1000, 100, uptime_start=start)
    col = CollectorProc(db, server.base_url)
    col.start()
    assert wait_for(lambda: has_series(db)), "baseline never collected"
    set_counts(server, 1600, 140, uptime_start=start)
    time.sleep(TWO_POLLS)
    set_counts(server, 1900, 160, uptime_start=start)
    time.sleep(TWO_POLLS)
    check("D1-AC2 computed_input total", total(db, COMPUTED_INPUT), 900)
    check("D1-AC2 generated_output total", total(db, GENERATED_OUTPUT), 60)
    check("D1-AC2 processed total", total(db, COMPUTED_INPUT) + total(db, GENERATED_OUTPUT), 960)

    rc, secs, _ = col.stop()
    check("D1-AC6 SIGTERM exit code", rc, 0)
    receipt("D1-AC6 SIGTERM stop seconds", round(secs, 2))
    assert secs <= 15, f"graceful exit took {secs:.1f}s"
    storage.acquire_volume_lock(db).release()  # proves the exit released the lock
    receipt("volume lock re-acquired after exit", True)

    col.start()
    time.sleep(TWO_POLLS)
    check("restart: totals unchanged (no restart credit)", total(db, COMPUTED_INPUT), 900)
    zero = q(
        db,
        "SELECT COUNT(*) AS n FROM samples WHERE source_id='s1' "
        "AND delta_computed_input=0 AND computed_input IS NOT NULL",
    )
    receipt("restart: observed-zero samples (D1-AC2 zero-not-missing)", zero[0]["n"])
    assert zero[0]["n"] >= 1

    set_counts(server, 1900, 160, ri=2000, ro_=100, uptime_start=start)
    time.sleep(POLL + 2)  # let the optional counters get a committed baseline
    set_counts(server, 1900, 160, ri=3000, ro_=140, uptime_start=start)
    time.sleep(TWO_POLLS)
    check("D1-AC5 request_input total (separate accounting)", total(db, REQUEST_INPUT), 1000)
    check("D1-AC5 primary stays engine-only", total(db, COMPUTED_INPUT), 900)
    rc, _, _ = col.stop()
    ticker.stop()
    check("scenario 1 clean exit", rc, 0)


def scenario_gap_and_disjoint(server: FixtureServer, db: Path) -> None:
    print("--- scenario 2: gap recovery credits once; disjoint evidence credits nothing")
    start = time.monotonic()
    reset_state(server)
    ticker = UptimeTicker(server)
    set_counts(server, 100, 10, uptime_start=start)
    col = CollectorProc(db, server.base_url)
    col.start()
    assert wait_for(lambda: has_series(db)), "baseline never collected"
    server.state.metrics_status = 500  # outage longer than 2 poll intervals
    time.sleep(TWO_POLLS + 2)
    server.state.metrics_status = None
    set_counts(server, 400, 40, uptime_start=start)
    time.sleep(2 * POLL)
    check("D1-AC3 gap recovery credited once", total(db, COMPUTED_INPUT), 300)
    gap_flags = q(
        db,
        "SELECT COUNT(*) AS n FROM samples WHERE source_id='s1' "
        "AND quality_json LIKE '%collection_gap%'",
    )
    receipt("gap-flagged recovery samples", gap_flags[0]["n"])
    assert gap_flags[0]["n"] >= 1
    rate = q(
        db,
        "SELECT COUNT(*) AS n FROM samples WHERE source_id='s1' "
        "AND quality_json LIKE '%collection_gap%' AND rate_eligible=1",
    )
    check("gap-flagged samples are never rate-eligible", rate[0]["n"], 0)
    time.sleep(TWO_POLLS)
    check("gap recovery not re-credited", total(db, COMPUTED_INPUT), 300)

    # restart across a gap: uptime evidence disjoint from the committed interval
    ticker.stop()
    server.state.metrics_status = 500
    time.sleep(TWO_POLLS + 2)
    server.state.metrics_status = None
    set_counts(server, 800, 80, uptime_fixed=5)
    time.sleep(2 * POLL)
    check("D1-AC3 disjoint evidence credits nothing", total(db, COMPUTED_INPUT), 300)
    reasons = [r["reason"] for r in q(db, "SELECT reason FROM epochs ORDER BY epoch_id")]
    receipt("epoch reasons", reasons)
    assert "restart_evidence" in reasons
    rc, _, _ = col.stop()
    check("scenario 2 clean exit", rc, 0)


def scenario_crash_atomicity(server: FixtureServer, db: Path) -> None:
    print("--- scenario 3: hard crash leaves no partial write; restart resumes")
    start = time.monotonic()
    reset_state(server)
    ticker = UptimeTicker(server)
    set_counts(server, 50, 5, uptime_start=start)
    col = CollectorProc(db, server.base_url)
    col.start()
    assert wait_for(lambda: has_series(db)), "baseline never collected"
    before = q(db, "SELECT COUNT(*) AS n FROM samples")[0]["n"]
    before_sum = total(db, COMPUTED_INPUT)
    server.state.delay_s = 2.0  # request in flight when we crash
    set_counts(server, 999, 99, uptime_start=start)
    col.kill_mid_poll()  # SIGKILL mid-poll
    server.state.delay_s = 0.0
    after = q(db, "SELECT COUNT(*) AS n FROM samples")[0]["n"]
    check("D1-AC4 crash: no partial sample write", after, before)
    check("D1-AC4 crash: hourly unchanged", total(db, COMPUTED_INPUT), before_sum)
    with ro(db) as conn:
        check("D1-AC4 crash: integrity_check", conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
    set_counts(server, 150, 15, uptime_start=start)
    col.start()
    time.sleep(TWO_POLLS)
    check("crash: resumed collection", total(db, COMPUTED_INPUT) > before_sum, True)
    rc, _, _ = col.stop()
    ticker.stop()
    check("scenario 3 clean exit", rc, 0)


def scenario_ownership(tmp: Path, server: FixtureServer, db: Path) -> None:
    print("--- scenario 4: second collector refused; corrupt/foreign files refused")
    col = CollectorProc(db, server.base_url)
    col.start()
    time.sleep(2)
    rc, stderr = CollectorProc(db, server.base_url).run_to_exit()
    receipt("second collector exit code", rc)
    check("second collector refused", "refusing to start" in stderr, True)
    col.stop()

    corrupt = tmp / "corrupt.sqlite3"
    shutil.copy(db, corrupt)
    data = bytearray(corrupt.read_bytes())
    half = len(data) // 2
    for i in range(half, min(half + 65536, len(data))):
        data[i] = (data[i] * 7 + 13) & 0xFF
    corrupt.write_bytes(bytes(data))
    rc2, err2 = CollectorProc(corrupt, server.base_url).run_to_exit()
    check("corrupt db refused", rc2, 3)
    receipt("corrupt db stderr head", err2.strip().splitlines()[:1])

    junk = tmp / "junk.sqlite3"
    junk.write_bytes(b"not a database at all" * 100)
    rc3, _ = CollectorProc(junk, server.base_url).run_to_exit()
    check("foreign file refused", rc3, 3)


def scenario_endpoint_and_history(server: FixtureServer, db: Path) -> None:
    print("--- scenario 5: endpoint change keeps history; retention keeps hourly")
    localhost_url = server.base_url.replace("127.0.0.1", "localhost")
    col = CollectorProc(db, localhost_url)
    col.start()
    time.sleep(TWO_POLLS)
    reasons = [r["reason"] for r in q(db, "SELECT reason FROM epochs ORDER BY epoch_id")]
    receipt("epoch reasons after endpoint change", reasons)
    assert "endpoint_change" in reasons, reasons
    check("lifetime totals preserved across endpoint change", total(db, COMPUTED_INPUT), 300)
    rc, _, _ = col.stop()
    check("scenario 5 clean exit", rc, 0)

    conn = storage.open_storage(db)
    hourly_before = q(db, "SELECT COUNT(*) AS n FROM hourly_totals")[0]["n"]
    removed = storage.purge_expired(conn, "2999-01-01T00:00:00.000000Z")
    samples_left = q(db, "SELECT COUNT(*) AS n FROM samples")[0]["n"]
    hourly_after = q(db, "SELECT COUNT(*) AS n FROM hourly_totals")[0]["n"]
    receipt("retention removed raw rows", removed)
    check("D1-AC5 retention purges raw samples", samples_left, 0)
    check("D1-AC5 retention keeps hourly rows", hourly_after, hourly_before)
    conn.close()


def scenario_transport(server: FixtureServer, tmp: Path) -> None:
    print("--- scenario 6: oversized response bounded; stats-only failure credited")
    reset_state(server)
    server.state.oversize = True
    db = tmp / "d6.sqlite3"
    col = CollectorProc(db, server.base_url)
    col.start()
    time.sleep(2 * POLL)
    rows = q(db, "SELECT last_error_code AS c FROM sources WHERE source_id='s1'")
    check("oversize bounded transport", rows[0]["c"] if rows else None, "response_too_large")
    check("no series fabricated from oversize", len(q(db, "SELECT 1 FROM series")), 0)
    col.stop()

    reset_state(server)
    install_fixture(server.state, L, 100, 10)
    server.state.stats_fail = True  # deltas accepted, continuity unverified (B10)
    start = time.monotonic()
    ticker6 = UptimeTicker(server)
    db2 = tmp / "d6b.sqlite3"
    col2 = CollectorProc(db2, server.base_url)
    col2.start()
    assert wait_for(lambda: has_series(db2)), "baseline never collected"
    set_counts(server, 250, 25, uptime_start=start)  # stats keep failing
    time.sleep(TWO_POLLS)
    check("B10 stats outage: deltas still credited", total(db2, COMPUTED_INPUT), 150)
    unverified = q(
        db2,
        "SELECT COUNT(*) AS n FROM samples WHERE source_id='s1' "
        "AND quality_json LIKE '%continuity_unverified%'",
    )
    receipt("continuity_unverified samples", unverified[0]["n"])
    assert unverified[0]["n"] >= 1
    rc, _, _ = col2.stop()
    ticker6.stop()
    check("scenario 6 clean exit", rc, 0)


class ServeProc(CollectorProc):
    """Real `serve` subprocess: collector writer plus the read API."""

    def __init__(self, db: Path, base_url: str, port: int):
        super().__init__(db, base_url)
        self.port = port
        self.env["JARVIS_BIND_ADDRESS"] = "127.0.0.1"
        self.env["PORT"] = str(port)

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "jarvis_dashboard.cli", "serve"],
            cwd=ROOT,
            env=self.env,
            stderr=subprocess.PIPE,
            text=True,
        )
        LAUNCHED.append(self)


def free_port() -> int:
    """A port nothing is listening on, so a stale process cannot fake receipts."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def api(port: int, path: str, query: str = "") -> tuple[int, dict, object]:
    """Headers are returned as an HTTPMessage: wire header names are case-insensitive."""
    import json
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/api/v1{path}"
    if query:
        url = f"{url}?{query}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read()), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


def get_path(port: int, path: str) -> tuple[int, dict]:
    """0 + code ``unreachable`` means the listener is not up yet."""
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    except urllib.error.URLError:
        return 0, {"error": {"code": "unreachable"}}


def raw_get(port: int, path: str) -> tuple[int, str]:
    """Status and content-type for a path whose body is not JSON (the built UI)."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, r.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("content-type", "")
    except urllib.error.URLError:
        return 0, "unreachable"


def scenario_serve_and_api(server: FixtureServer, tmp: Path) -> None:
    print("--- scenario 7: real serve, bounded API, purge-stable history")
    port = free_port()
    db = tmp / "d7.sqlite3"
    reset_state(server)
    install_fixture(server.state, L, 1000, 100)
    start = time.monotonic()
    ticker = UptimeTicker(server)
    serve = ServeProc(db, server.base_url, port)
    serve.start()

    ready_at = None
    deadline = time.monotonic() + 30
    first = None
    while time.monotonic() < deadline:
        status, body = get_path(port, "/health/ready")
        if first is None:
            first = (status, body.get("error", {}).get("code") or body.get("status"))
        if status == 200:
            ready_at = time.monotonic() - start
            break
        if serve.proc.poll() is not None:
            err = serve.proc.stderr.read() if serve.proc.stderr else ""
            raise AssertionError(f"serve exited early rc={serve.proc.returncode}: {err[-400:]}")
        time.sleep(0.5)
    check("readiness turned ready", ready_at is not None, True)
    receipt("readiness first state", first)
    receipt("seconds to ready", round(ready_at, 2) if ready_at else None)
    receipt("live during startup (1 = served, 0 = not listening yet)",
            get_path(port, "/health/live")[0])

    set_counts(server, 1600, 140, 1000, 100, uptime_start=start)
    time.sleep(TWO_POLLS)
    set_counts(server, 1900, 160, 1000, 100, uptime_start=start)
    assert wait_for(lambda: total(db, COMPUTED_INPUT) == 900, 40), "A2 chain not collected"

    status, series, headers = api(port, "/series")
    check("series page", status, 200)
    check("no-store on API", headers.get("cache-control"), "no-store")
    series_id = series["items"][0]["series_id"]
    receipt("series items", len(series["items"]))

    status, summary, _ = api(port, "/summary", f"series_id={series_id}")
    check("summary status", status, 200)
    life = summary["ranges"]["lifetime"]
    check("lifetime computed_input over HTTP", life["counters"]["computed_input"]["tokens"], "900")
    check("lifetime generated_output over HTTP", life["counters"]["generated_output"]["tokens"], "60")
    check("lifetime processed_total over HTTP", life["processed_total"], "960")
    receipt("summary schema_version", summary["schema_version"])
    receipt("collection_status", summary["collection_status"])

    first_hour, last_hour = hour_window(db)
    iso = f"{first_hour[:-1]}:00:00Z"
    end = next_hour(last_hour)
    status, history, _ = api(
        port, "/history", f"series_id={series_id}&from={iso}&to={end}&bucket=hour"
    )
    check("history status", status, 200)
    expected_buckets = (
        int((_dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
             - _dt.datetime.fromisoformat(iso)).total_seconds()) // 3600
    )
    check("history generates every bucket in range", history["bucket_count"], expected_buckets)
    receipt("history buckets", history["bucket_count"])
    check(
        "history buckets sum to the lifetime total",
        str(sum(int(b["counters"]["computed_input"]["tokens"] or 0)
                for b in history["buckets"])),
        "900",
    )
    receipt(
        "history trailing bucket gap_seconds",
        history["buckets"][-1]["counters"]["computed_input"]["gap_seconds"],
    )

    # A counter reset gives the events route real data and keeps the reset distinct
    # from a plain increment (the reset poll itself credits nothing).
    set_counts(server, 10, 5, 1000, 100, uptime_start=start)
    assert wait_for(lambda: epoch_reasons(db) [:1] == ["counter_reset"], 25), "reset not recorded"
    receipt("epoch reasons (newest first)", epoch_reasons(db))
    status, events, _ = api(port, "/events", f"series_id={series_id}&limit=5")
    check("events page", status, 200)
    check(
        "events describe the real reset",
        [item["kind"] for item in events["items"]][:1],
        ["counter_reset"],
    )
    receipt("events retention note", events["retention"]["note"])
    receipt("openapi paths", len(get_path(port, "/api/v1/openapi.json")[1]["paths"]))
    root_status, root_type = raw_get(port, "/")
    receipt("root response", f"{root_status} {root_type.split(';')[0] or 'no content-type'}")

    # 32 concurrent reads against 4 admission slots: saturating requests are refused
    # promptly with the documented code, nothing queues, and the server stays usable.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(lambda _: api(port, "/status"), range(32)))
    codes = {status for status, _, _ in outcomes}
    receipt("concurrent status codes", sorted(codes))
    if not codes <= {200, 503}:
        raise AssertionError(f"unexpected concurrent codes: {sorted(codes)}")
    served = [body for status, body, _ in outcomes if status == 200]
    refused = [body for status, body, _ in outcomes if status == 503]
    if not served:
        raise AssertionError("every concurrent read was refused")
    bad = [body.get("error", {}).get("code") for body in refused
           if body.get("error", {}).get("code") != "read_capacity_exceeded"]
    if bad:
        raise AssertionError(f"refusal without the documented code: {bad[:2]}")
    receipt("concurrent served / refused", (len(served), len(refused)))
    check("usable again after the burst", api(port, "/status")[0], 200)

    # Purging raw samples must not change what the API reports.
    serve.stop()
    conn = storage.open_storage(db)
    purged = storage.purge_expired(conn, "2099-01-01T00:00:00.000000Z")
    conn.commit()
    conn.close()
    receipt("raw rows purged", purged)
    serve.start()
    assert wait_for(lambda: get_path(port, "/health/ready")[0] == 200, 30), "no readiness after purge"
    status, summary, _ = api(port, "/summary", f"series_id={series_id}")
    check(
        "lifetime after raw purge",
        summary["ranges"]["lifetime"]["counters"]["computed_input"]["tokens"],
        "900",
    )
    status, history, _ = api(
        port, "/history", f"series_id={series_id}&from={iso}&to={end}&bucket=hour"
    )
    check(
        "history coverage metadata survives purge",
        [b["counters"]["computed_input"]["metadata_complete"] for b in history["buckets"]],
        [True] * history["bucket_count"],
    )
    check(
        "history first/last observations survive purge",
        history["buckets"][0]["counters"]["computed_input"]["first_observed_at"] is not None,
        True,
    )
    rc, secs, _ = serve.stop()
    check("serve SIGTERM exit", rc, 0)
    check("serve shutdown under 15 s", secs < 15, True)
    receipt("serve shutdown seconds", round(secs, 2))
    ticker.stop()


def scenario_backup_restore(server: FixtureServer, tmp: Path) -> None:
    print("--- scenario 8: live snapshot, stopped restore, gap-free continuation")
    db = tmp / "d8.sqlite3"
    reset_state(server)
    install_fixture(server.state, L, 1000, 100)
    start = time.monotonic()
    ticker = UptimeTicker(server)
    col = CollectorProc(db, server.base_url)
    col.start()
    assert wait_for(lambda: has_series(db)), "baseline not collected"
    set_counts(server, 1600, 140, 1000, 100, uptime_start=start)
    time.sleep(TWO_POLLS)
    set_counts(server, 1900, 160, 1000, 100, uptime_start=start)
    assert wait_for(lambda: total(db, COMPUTED_INPUT) == 900, 40), "A2 chain not collected"

    snapshot = tmp / "snap.sqlite3"
    env = {**os.environ, "RADIANCE_BASE_URL": server.base_url, "SOURCE_ID": "s1",
           "DATABASE_PATH": str(db), "LOG_LEVEL": "WARNING"}
    result = subprocess.run(
        [sys.executable, "-m", "jarvis_dashboard.cli", "backup", "--output", str(snapshot)],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    check("backup exit code", result.returncode, 0)
    receipt("backup receipt", result.stdout.strip()[:78])
    receipt("live volume has WAL while running", Path(f"{db}-wal").exists())
    check("snapshot ships without WAL", Path(f"{snapshot}-wal").exists(), False)

    refused = subprocess.run(
        [sys.executable, "-m", "jarvis_dashboard.cli", "backup", "--output", str(snapshot)],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    check("backup refuses to overwrite", refused.returncode, 3)
    receipt("overwrite refusal", refused.stderr.strip().splitlines()[-1][:70])

    # Restore into a fresh, stopped volume, then continue collecting there.
    target = tmp / "restored" / "dashboard.sqlite3"
    target.parent.mkdir()
    restore_env = {**env, "DATABASE_PATH": str(target)}
    restore = subprocess.run(
        [sys.executable, "-m", "jarvis_dashboard.cli", "restore", "--input", str(snapshot)],
        cwd=ROOT, env=restore_env, capture_output=True, text=True,
    )
    check("restore exit code", restore.returncode, 0)
    receipt("restore receipt", restore.stdout.strip()[:78])
    check("restored lifetime matches snapshot", total(target, COMPUTED_INPUT), 900)
    check("restored processed pair", total(target, GENERATED_OUTPUT), 60)
    events = q(target, "SELECT identity, details_json FROM events WHERE kind='restore'")
    check("restore event recorded once", len(events), 1)
    check("restore event is source-wide", events[0]["identity"], None)
    receipt("restore provenance keys", sorted(__import__("json").loads(events[0]["details_json"])))

    rc, _, _ = col.stop()
    check("collector stopped before running-target check", rc, 0)

    # A running target refuses restore: lock is held by the live writer.
    col2 = CollectorProc(target, server.base_url)
    col2.start()
    assert wait_for(lambda: volume_lock_held(target)), "writer never took the volume lock"
    busy = subprocess.run(
        [sys.executable, "-m", "jarvis_dashboard.cli", "restore", "--input", str(snapshot)],
        cwd=ROOT, env=restore_env, capture_output=True, text=True,
    )
    check("restore refuses a running target", busy.returncode, 3)
    receipt("running-target refusal", busy.stderr.strip().splitlines()[-1][:70])

    # Continuation: one new interval only, no duplicated increments.
    set_counts(server, 2000, 170, 1000, 100, uptime_start=start)
    assert wait_for(lambda: total(target, COMPUTED_INPUT) == 1000, 40), "continuation not applied"
    check("continuation adds one interval", total(target, COMPUTED_INPUT), 1000)
    check("no duplicated generated output", total(target, GENERATED_OUTPUT), 70)
    check("optional counters unchanged", total(target, REQUEST_INPUT), 0)
    rc, _, _ = col2.stop()
    check("restored volume collector exits", rc, 0)
    ticker.stop()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-smoke-"))
    print(f"smoke workdir: {tmp}")
    try:
        run_scenarios(tmp)
    finally:
        stop_all()
    shutil.rmtree(tmp, ignore_errors=True)
    print("SMOKE PASS")
    return 0


def run_scenarios(tmp: Path) -> None:
    with FixtureServer() as server:
        install_fixture(server.state, L, 1000, 100)
        scenario_normal_and_restart(server, tmp / "d1.sqlite3")
        scenario_gap_and_disjoint(server, tmp / "d2.sqlite3")
        scenario_crash_atomicity(server, tmp / "d3.sqlite3")
        scenario_ownership(tmp, server, tmp / "d3.sqlite3")
        scenario_endpoint_and_history(server, tmp / "d2.sqlite3")
        scenario_transport(server, tmp)
        scenario_serve_and_api(server, tmp)
        scenario_backup_restore(server, tmp)


if __name__ == "__main__":
    sys.exit(main())
