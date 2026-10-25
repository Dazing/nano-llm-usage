from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis_dashboard import storage
from jarvis_dashboard.collector import Collector
from jarvis_dashboard.config import Config
from jarvis_dashboard.domain import (
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    PRIMARY_SCOPE,
    canonical_labels_json,
)

from fixture_server import FixtureServer, install_fixture

T0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
L = {"model_name": "fixture-model", "engine": "0"}
IDENT = canonical_labels_json(L)


class Clock:
    def __init__(self) -> None:
        self.wall = T0
        self.mono = 0.0

    def wall_now(self) -> datetime:
        return self.wall

    def mono_now(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def make_config(base_url: str, db: Path, poll: int = 15) -> Config:
    return Config(base_url, "s1", poll, 3.0, str(db), 30, 8080, "INFO")


def make_collector(server: FixtureServer, db: Path, clock: Clock, poll: int = 15, sleeper=None) -> Collector:
    conn = storage.open_storage(db)
    return Collector(
        make_config(server.base_url, db, poll), conn,
        wall_clock=clock.wall_now, monotonic=clock.mono_now, sleeper=sleeper,
    )


def set_uptime(server: FixtureServer, clock: Clock, base: float = 100.0) -> None:
    server.state.uptime_s = base + (clock.wall - T0).total_seconds()


def hourly(conn, counter=COMPUTED_INPUT):
    return conn.execute(
        "SELECT hour, tokens, observed_intervals FROM hourly_totals WHERE counter_key=? ORDER BY hour",
        (counter,),
    ).fetchall()


def test_end_to_end_normal_usage_and_restart(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 1000, 100)
        clock = Clock()
        c = make_collector(server, db, clock)

        set_uptime(server, clock)
        assert c.poll_once("p1").ok
        assert hourly(c.conn) == []  # D1-AC1: baseline alone has no observed interval

        set_uptime(server, clock)
        clock.advance(15)
        set_uptime(server, clock)
        install_fixture(server.state, L, 1600, 140)
        c.poll_once("p2")
        clock.advance(15)
        set_uptime(server, clock)
        install_fixture(server.state, L, 1900, 160)
        c.poll_once("p3")
        # D1-AC2: 900 input + 60 output = 960 processed
        assert hourly(c.conn) == [("2026-01-15T10Z", 900, 2)]
        assert hourly(c.conn, GENERATED_OUTPUT) == [("2026-01-15T10Z", 60, 2)]

        # restart on the same path; unchanged counters keep 960 and observe a zero
        c.conn.close()
        c2 = make_collector(server, db, clock)
        clock.advance(15)
        set_uptime(server, clock)
        c2.poll_once("p4")
        assert hourly(c2.conn) == [("2026-01-15T10Z", 900, 3)]
        row = c2.conn.execute(
            "SELECT delta_computed_input FROM samples ORDER BY sample_id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == 0

        # D1-AC4: optional request counters contribute separately
        install_fixture(server.state, L, 1900, 160, ri=2000, ro=100)
        clock.advance(15)
        set_uptime(server, clock)
        c2.poll_once("p5")
        install_fixture(server.state, L, 1900, 160, ri=3000, ro=140)
        clock.advance(15)
        set_uptime(server, clock)
        c2.poll_once("p6")
        assert hourly(c2.conn, COMPUTED_INPUT) == [("2026-01-15T10Z", 900, 5)]
        assert hourly(c2.conn, "request_input") == [("2026-01-15T10Z", 1000, 1)]
        c2.conn.close()


def test_metrics_failure_then_gap_recovery(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 100, 10)
        clock = Clock()
        c = make_collector(server, db, clock)
        set_uptime(server, clock)
        c.poll_once("p1")

        server.state.metrics_status = 500  # outage: no metrics at all
        clock.advance(15)
        report = c.poll_once("p2")
        assert not report.ok and report.error_code == "http_status_500"
        assert c.conn.execute("SELECT last_error_code FROM sources").fetchone()[0] == "http_status_500"
        # baselines unchanged, tokens unchanged
        st = storage.load_series(c.conn, "s1")[IDENT].state
        assert st.primary.computed_input == 100

        clock.advance(165)
        set_uptime(server, clock)
        server.state.metrics_status = None
        install_fixture(server.state, L, 400, 40)
        report = c.poll_once("p3")
        assert report.ok
        # recovery credits 300/30 exactly once with gap flags and null rate
        assert hourly(c.conn) == [("2026-01-15T10Z", 300, 1)]
        q = json.loads(c.conn.execute("SELECT quality_json FROM hourly_totals").fetchone()[0])
        assert "collection_gap" in q
        assert c.conn.execute("SELECT rate_eligible FROM samples ORDER BY sample_id DESC LIMIT 1").fetchone()[0] == 0
        c.conn.close()


def test_stats_only_failure_marks_unverified(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 100, 10)
        clock = Clock()
        c = make_collector(server, db, clock)
        set_uptime(server, clock)
        c.poll_once("p1")
        server.state.stats_fail = True
        clock.advance(15)
        install_fixture(server.state, L, 150, 15)
        assert c.poll_once("p2").ok
        q = json.loads(c.conn.execute(
            "SELECT quality_json FROM samples ORDER BY sample_id DESC LIMIT 1").fetchone()[0])
        assert "continuity_unverified" in q
        assert hourly(c.conn) == [("2026-01-15T10Z", 50, 1)]  # deltas still accepted
        c.conn.close()


def test_series_disappearance_and_reappearance(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 100, 10)
        clock = Clock()
        c = make_collector(server, db, clock)
        set_uptime(server, clock)
        c.poll_once("p1")
        server.state.drop_series(L)
        clock.advance(15)
        set_uptime(server, clock)
        c.poll_once("p2")
        st = storage.load_series(c.conn, "s1")[IDENT].state
        assert PRIMARY_SCOPE in st.open_gaps and st.stale_since is not None
        assert c.conn.execute("SELECT stale_since FROM series").fetchone()[0] is not None
        clock.advance(15)
        set_uptime(server, clock)
        install_fixture(server.state, L, 130, 13)
        c.poll_once("p3")
        assert hourly(c.conn) == [("2026-01-15T10Z", 30, 1)]  # credited once after gap
        st = storage.load_series(c.conn, "s1")[IDENT].state
        assert st.open_gaps == {} and st.stale_since is None
        c.conn.close()


def test_long_gap_materialization_is_bounded_and_resumable(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 100, 10)
        clock = Clock()
        c = make_collector(server, db, clock)
        set_uptime(server, clock)
        c.poll_once("p1")
        server.state.metrics_status = 500
        clock.advance(15)
        c.poll_once("p2")
        # 30 days of downtime -> >700 hourly pieces, beyond the per-poll batch
        clock.advance(30 * 24 * 3600)
        set_uptime(server, clock)
        server.state.metrics_status = None
        install_fixture(server.state, L, 150, 15)
        c.poll_once("p3")
        remaining = storage.drain_gap_materialization(c.conn, batch=100)
        while remaining:
            remaining = storage.drain_gap_materialization(c.conn, batch=100)
        rows = c.conn.execute(
            "SELECT hour FROM hourly_totals WHERE quality_json LIKE '%collection_gap%' ORDER BY hour"
        ).fetchall()
        hours = [r[0] for r in rows]
        assert len(hours) == len(set(hours))  # no duplicate overlap per hour
        assert len(hours) == 30 * 24 + 1  # full coverage of the outage
        c.conn.close()


def test_retention_keeps_hourly_totals(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 100, 10)
        clock = Clock()
        cfg = make_config(server.base_url, db)
        conn = storage.open_storage(db)
        c = Collector(cfg, conn, wall_clock=clock.wall_now, monotonic=clock.mono_now)
        set_uptime(server, clock)
        c.poll_once("p1")
        c.poll_once("p2")  # p2 at same instant: elapsed 0, no deltas
        clock.advance(1)
        before = hourly(conn)
        conn.execute(
            "INSERT INTO maintenance_cursor(key, value) VALUES('last_retention_at', '2000-01-01T00:00:00.000000Z')"
        )
        # shrink retention so everything is expired; maintenance runs purge
        object.__setattr__(cfg, "raw_retention_days", 0)
        c._maintenance()
        assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 0
        assert hourly(conn) == before  # hourly totals survive (C23)
        conn.close()


def test_run_forever_skips_missed_slots_without_burst(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 100, 10)
        clock = Clock()
        sleeps = iter([15.0, 20.0, 15.0, 15.0, 15.0])

        def sleeper(delay):
            clock.advance(next(sleeps))
            if clock.mono >= 70:
                c.request_stop()

        c = make_collector(server, db, clock, poll=15, sleeper=sleeper)
        polls = []
        real_poll = c.poll_once

        def spy(poll_id):
            polls.append(poll_id)
            return real_poll(poll_id)

        c.poll_once = spy  # type: ignore[method-assign]
        c.run_forever()
        assert len(polls) == 5  # one poll per slot, no queued burst after the slow slot
        assert len(set(polls)) == 5
        c.conn.close()


def test_cli_sigterm_stops_and_releases_lock(tmp_path: Path):
    with FixtureServer() as server:
        db = tmp_path / "d.sqlite3"
        install_fixture(server.state, L, 100, 10)
        env = {
            **__import__("os").environ,
            "RADIANCE_BASE_URL": server.base_url,
            "SOURCE_ID": "s1",
            "POLL_INTERVAL_SECONDS": "5",
            "DATABASE_PATH": str(db),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "jarvis_dashboard.cli", "collect"],
            env=env, cwd=str(Path(__file__).parent.parent),
            stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if db.exists() and any(
                    storage.load_series(storage.open_storage(db), "s1").values()
                ):
                    break
                time.sleep(0.3)
            else:
                pytest.fail("collector never persisted the baseline series")
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=15)  # D1-AC6: graceful exit within 15 seconds
            assert rc == 0
        finally:
            proc.kill()
        lock = storage.acquire_volume_lock(db)  # lock was released
        lock.release()


def test_first_poll_metrics_failure_records_attempt_only(tmp_path: Path):
    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        server.state.metrics_status = 500
        clock = Clock()
        c = make_collector(server, db, clock)
        report = c.poll_once("p1")
        assert not report.ok
        assert storage.load_series(c.conn, "s1") == {}  # no fabricated series
        assert c.conn.execute(
            "SELECT last_error_code FROM sources WHERE source_id='s1'"
        ).fetchone()[0] == "http_status_500"
        c.conn.close()


def test_sigterm_during_active_request_exits_promptly(tmp_path: Path):
    import threading

    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 100, 10)
        server.state.delay_s = 2.0  # every request stays "active" for 2s
        clock = Clock()
        c = make_collector(server, db, clock)
        started = time.monotonic()
        # stop arrives mid in-flight first poll (same-thread DB ownership kept)
        threading.Timer(0.5, c.request_stop).start()
        c.run_forever()  # returns when the loop exits; hangs would time the test out
        assert time.monotonic() - started < 15  # D1-AC6 graceful exit window
        assert storage.load_series(c.conn, "s1")  # in-flight poll committed
        c.conn.close()


def test_read_only_storage_fails_gracefully_without_state_advance(tmp_path: Path):
    # Scenario D28: write failures keep committed state intact
    import os

    db = tmp_path / "d.sqlite3"
    with FixtureServer() as server:
        install_fixture(server.state, L, 100, 10)
        clock = Clock()
        c = make_collector(server, db, clock)
        assert c.poll_once("p1").ok
        baseline = storage.load_series(c.conn, "s1")[canonical_labels_json(L)].state
        server.state.drop_series(L)
        install_fixture(server.state, L, 200, 20)
        # Re-open after revoking write access: an existing fd ignores mode bits.
        c.conn.close()
        os.chmod(db, 0o444)
        os.chmod(db.parent, 0o555)
        try:
            import sqlite3 as _sq

            import pytest

            c.conn = _sq.connect(db)
            # A WAL database on read-only media cannot open its write-ahead
            # index; every write path fails and run_forever marks the storage
            # unhealthy (covered separately). Here: nothing is committed.
            with pytest.raises(_sq.OperationalError):
                c.poll_once("p2")
        finally:
            os.chmod(db.parent, 0o755)
            os.chmod(db, 0o644)
        after = storage.load_series(c.conn, "s1")[canonical_labels_json(L)].state
        assert (after.primary.computed_input, after.primary.generated_output) == (100, 10)
        c.conn.close()
