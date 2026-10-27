"""Does an absent -> returning counter cycle credit tokens it never earned?

Reproduces the frontend suite's keeper shape: the fixture drops and re-publishes the
series every 20 s, so the collector alternates "series missing" / "series present"
with unchanged counter values. A priced hour must not grow from those cycles.
"""
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "/workspace/src")

from jarvis_dashboard import queries, storage  # noqa: E402
from jarvis_dashboard.accounting import transition_series  # noqa: E402
from jarvis_dashboard.config import CostRates  # noqa: E402
from jarvis_dashboard.domain import (  # noqa: E402
    ScrapeCounters,
    StatsEvidence,
    canonical_labels_json,
)
from jarvis_dashboard.lifecycle import RuntimeHealth  # noqa: E402

SOURCE = "smoke"
LABELS = {"model_name": "fixture-model", "engine": "0"}
T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)
UP = datetime(2026, 10, 27, 9, 58, 0, tzinfo=UTC)
RATES = CostRates(Decimal("3"), Decimal("0.30"), Decimal("15"))


def scrape(ci, go, ri, ro, ca):
    values = {
        "computed_input": ci,
        "generated_output": go,
        "request_input": ri,
        "request_output": ro,
        "cached_input": ca,
    }
    return ScrapeCounters(LABELS, values, {})


def main() -> None:
    db = Path(tempfile.mkdtemp()) / "d.sqlite3"
    conn = storage.open_storage(db)
    series_id = queries.encode_series_id(SOURCE, canonical_labels_json(LABELS))
    state = None
    at = T0
    poll = 5

    def apply(counters, label):
        nonlocal state, at
        plan = transition_series(
            state,
            counters,
            at,
            StatsEvidence("fixture-model", (at - UP).total_seconds(), UP, at),
            poll,
        )
        storage.apply_poll(
            conn,
            source_id=SOURCE,
            endpoint_fingerprint="fp",
            poll_id=f"p-{label}-{at:%H%M%S}",
            observed_at=at,
            plans=[plan],
        )
        state = plan.state

    def cost(label):
        health = RuntimeHealth(poll_interval_seconds=poll)
        health.mark_started(T0)
        health.mark_writer_ready()
        health.mark_primary_success(at)
        payload = queries.read_summary(
            db,
            source_id=SOURCE,
            series_id=series_id,
            epoch_limit=20,
            epoch_cursor=None,
            health=health.snapshot(at),
            now=at + timedelta(seconds=poll),
            poll_interval_seconds=poll,
            cost_rates=RATES,
        )
        item = payload["ranges"]["lifetime"]["api_equivalent"]
        print(f"{label:22s} amount={item['amount']} priced={item['priced_hours']}"
              f"/{item['expected_hours']} tokens={item['tokens']}")

    apply(scrape(1905, 162, 100, 100, 600_000), "baseline")
    at += timedelta(seconds=poll)
    apply(scrape(2000, 170, 1_000_100, 100_100, 1_200_000), "priced jump")
    cost("after jump")

    # Keeper shape: the scrape is missing for one poll, then the same values return.
    for cycle in range(10):
        at += timedelta(seconds=poll)
        apply(ScrapeCounters(LABELS, {}, {"absent": True}), f"absent{cycle}")
        at += timedelta(seconds=poll)
        apply(scrape(2000, 170, 1_000_100, 100_100, 1_200_000), f"return{cycle}")
    cost("after 10 drop cycles")

    # Same, with the whole series absent from the scrape (the collector's absent path).
    for cycle in range(10):
        at += timedelta(seconds=poll)
        apply(ScrapeCounters(LABELS, {}, {"absent": True}), f"gone{cycle}")
        at += timedelta(seconds=poll * 4)
        apply(scrape(2000, 170, 1_000_100, 100_100, 1_200_000), f"back{cycle}")
    cost("after 10 long drops")


main()
