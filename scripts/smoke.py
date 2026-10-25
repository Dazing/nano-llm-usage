#!/usr/bin/env python3
"""End-to-end smoke runner for Delivery 1 (plan §5 scenarios, numeric receipts).

Runs the controllable upstream fixture (tests/fixtures), the real collector CLI
as subprocesses, and reads SQLite back read-only. Every check prints a numeric
receipt; the script exits non-zero on the first failed expectation.

Usage: .venv/bin/python scripts/smoke.py
"""
from __future__ import annotations

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
    if time.monotonic() - BUDGET_START > 420:
        raise AssertionError("smoke budget exceeded")


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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-smoke-"))
    print(f"smoke workdir: {tmp}")
    with FixtureServer() as server:
        install_fixture(server.state, L, 1000, 100)
        scenario_normal_and_restart(server, tmp / "d1.sqlite3")
        scenario_gap_and_disjoint(server, tmp / "d2.sqlite3")
        scenario_crash_atomicity(server, tmp / "d3.sqlite3")
        scenario_ownership(tmp, server, tmp / "d3.sqlite3")
        scenario_endpoint_and_history(server, tmp / "d2.sqlite3")
        scenario_transport(server, tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
