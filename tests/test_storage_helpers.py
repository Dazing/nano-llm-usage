"""Shared durable-seed helpers for Delivery 2 storage/API tests.

Seeds one source ``s1`` with the acceptance-scenario A1/A2 shape: opening
balance (100,10) at 10:00:00Z then (400,40) at 10:03:00Z, i.e. one accepted
observed interval of 300/30 tokens in hour ``2026-01-15T10Z``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from jarvis_dashboard import storage
from jarvis_dashboard.accounting import transition_series
from jarvis_dashboard.domain import (
    ScrapeCounters,
    StatsEvidence,
    canonical_labels_json,
    to_iso,
)

T0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
LABELS = {"model_name": "m", "engine": "0"}
IDENT = canonical_labels_json(LABELS)
SOURCE = "s1"


def counters(ci, go, ri=None, ro=None) -> ScrapeCounters:
    return ScrapeCounters(
        dict(LABELS),
        {"computed_input": ci, "generated_output": go, "request_input": ri, "request_output": ro},
        {},
    )


def stats(at: datetime, uptime: float) -> StatsEvidence:
    return StatsEvidence("m", uptime, at, at)


def apply(conn, poll_id, plans, at=T0):
    return storage.apply_poll(
        conn,
        source_id=SOURCE,
        endpoint_fingerprint="fp1",
        poll_id=poll_id,
        observed_at=at,
        plans=plans,
    )


def plan1():
    return transition_series(None, counters(100, 10), T0, stats(T0, 100), 15)


def plan2(prev=None):
    state = prev if prev is not None else plan1().state
    at = T0 + timedelta(seconds=180)
    return transition_series(state, counters(400, 40), at, stats(at, 280), 15)


def apply_seed(conn) -> sqlite3.Connection:
    """Apply the A1/A2 seed; returns the connection for chaining."""
    apply(conn, "p1", [plan1()])
    apply(conn, "p2", [plan2()], T0 + timedelta(seconds=180))
    return conn


def hourly_totals(conn: sqlite3.Connection) -> dict[str, int]:
    """Summed hourly tokens per counter key (NULL token rows excluded)."""
    rows = conn.execute(
        "SELECT counter_key, tokens FROM hourly_totals WHERE tokens IS NOT NULL"
    ).fetchall()
    out: dict[str, int] = {}
    for key, tokens in rows:
        out[key] = out.get(key, 0) + tokens
    return out


def seed_chain(
    conn,
    model: str,
    steps: list[tuple[int, int]],
    start: datetime,
    *,
    times: list[datetime] | None = None,
    poll: int = 15,
    ri_base: int = 100,
    ro_base: int = 10,
    ri_step: int = 1,
    ro_step: int = 1,
    ca_base: int | None = None,
    ca_step: int = 1,
    uptime_base: float = 1000,
) -> None:
    """Write one continuous chain for a series so hourly buckets exist.

    Uptime follows the wall clock, so the chain stays continuity-verified;
    pass ``times`` to model outages and hour-boundary crossings. ``ca_base``
    publishes the optional cached-input counter; leaving it out models an
    upstream that has no cache counter at all.
    """
    labels = {"model_name": model, "engine": "0"}
    state = None
    for index, (ci, go) in enumerate(steps):
        at = start + timedelta(seconds=poll * index) if times is None else times[index]
        values = {
            "computed_input": ci,
            "generated_output": go,
            "request_input": ri_base + ri_step * index,
            "request_output": ro_base + ro_step * index,
        }
        if ca_base is not None:
            values["cached_input"] = ca_base + ca_step * index
        scrape = ScrapeCounters(labels, values, {})
        evidence = StatsEvidence(model, uptime_base + (at - start).total_seconds(), at, at)
        plan = transition_series(state, scrape, at, evidence, poll)
        apply(conn, f"{model}-p{index}", [plan], at)
        state = plan.state


def resume_chain(
    conn,
    model: str,
    steps: list[tuple[int, int]],
    start: datetime,
    *,
    poll: int = 15,
    ri_base: int = 102,
    ro_base: int = 12,
    ri_step: int = 0,
    ro_step: int = 0,
    ca_base: int | None = None,
    ca_step: int = 0,
    uptime_base: float = 1000,
) -> None:
    """Continue an existing series chain (the durable state is the baseline)."""
    labels = {"model_name": model, "engine": "0"}
    identity = canonical_labels_json(labels)
    state = storage.load_series(conn, SOURCE)[identity].state
    for index, (ci, go) in enumerate(steps):
        at = start + timedelta(seconds=poll * index)
        values = {
            "computed_input": ci,
            "generated_output": go,
            "request_input": ri_base + ri_step * index,
            "request_output": ro_base + ro_step * index,
        }
        if ca_base is not None:
            values["cached_input"] = ca_base + ca_step * index
        scrape = ScrapeCounters(labels, values, {})
        evidence = StatsEvidence(model, uptime_base + (at - T0).total_seconds(), at, at)
        plan = transition_series(state, scrape, at, evidence, poll)
        apply(conn, f"{model}-resume-{to_iso(at)}", [plan], at)
        state = plan.state


def downgrade_to_v1(conn) -> None:
    """Reduce a current-schema database to the shape Delivery 1 shipped.

    The data is written by the real writer, then the 0002 additions are
    removed, which is exactly the legacy state the migration and its coverage
    backfill must handle.
    """
    conn.execute("BEGIN IMMEDIATE")
    for name in (
        "idx_series_enum",
        "idx_events_series_order",
        "idx_gap_scope",
        "idx_epochs_series_order",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS latest_interval")
    for column in ("cached_input", "delta_cached_input"):
        conn.execute(f"ALTER TABLE samples DROP COLUMN {column}")
    for column in ("first_observed_at", "last_observed_at", "metadata_complete"):
        conn.execute(f"ALTER TABLE hourly_totals DROP COLUMN {column}")
    for column in ("seq", "first_valid_at", "last_valid_at"):
        conn.execute(f"ALTER TABLE series DROP COLUMN {column}")
    for column in ("first_success_at", "last_success_at"):
        conn.execute(f"ALTER TABLE sources DROP COLUMN {column}")
    conn.execute("DELETE FROM maintenance_cursor")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
