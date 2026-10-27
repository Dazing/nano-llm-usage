"""Replay the frontend keeper pattern: one priced jump, then identical republishes.

Question: can the same +1,000,000/+600,000/+100,000 jump be credited more than
once (which is the only shape that yields a 10x amount at an unchanged 0.6 ratio)?
"""
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/workspace/src")
sys.path.insert(0, "/workspace/tests")

from jarvis_dashboard import storage  # noqa: E402
from jarvis_dashboard.accounting import transition_series  # noqa: E402
from jarvis_dashboard.config import CostRates  # noqa: E402
from jarvis_dashboard.domain import ScrapeCounters, StatsEvidence, canonical_labels_json  # noqa: E402
from jarvis_dashboard import queries  # noqa: E402
from jarvis_dashboard.lifecycle import RuntimeHealth  # noqa: E402

from decimal import Decimal

SOURCE = "smoke"
LABELS = {"model_name": "fixture-model", "engine": "0"}
T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)
RATES = CostRates(Decimal("3"), Decimal("0.30"), Decimal("15"))
UP = datetime(2026, 10, 27, 9, 58, 0, tzinfo=UTC)


def scrape(ci, go, ri, ro, ca):
    values = {
        "computed_input": ci,
        "generated_output": go,
        "request_input": ri,
        "request_output": ro,
    }
    if ca is not None:
        values["cached_input"] = ca
    return ScrapeCounters(LABELS, values, {})


def stats(at):
    return StatsEvidence("fixture-model", (at - UP).total_seconds(), UP, at)


def _health(at: datetime):
    state = RuntimeHealth(poll_interval_seconds=5)
    state.mark_started(T0)
    state.mark_writer_ready()
    state.mark_primary_success(at)
    return state.snapshot(at)


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "d.sqlite3"
    conn = storage.open_storage(db)
    state = None
    series_id = queries.encode_series_id(SOURCE, canonical_labels_json(LABELS))

    steps = [scrape(1905, 162, 100, 100, 600_000)]
    steps.append(scrape(2000, 170, 1_000_100, 100_100, 1_200_000))
    for _ in range(12):
        steps.append(scrape(2000, 170, 1_000_100, 100_100, 1_200_000))

    at = T0
    for index, counters in enumerate(steps):
        plan = transition_series(state, counters, at, stats(at), 5)
        storage.apply_poll(
            conn,
            source_id=SOURCE,
            endpoint_fingerprint="fp",
            poll_id=f"p{index}",
            observed_at=at,
            plans=[plan],
        )
        state = plan.state
        now = at + timedelta(seconds=5)
        payload = queries.read_summary(
            db,
            source_id=SOURCE,
            series_id=series_id,
            epoch_limit=20,
            epoch_cursor=None,
            health=_health(now),
            now=now,
            poll_interval_seconds=5,
            cost_rates=RATES,
        )
        cost = payload["ranges"]["lifetime"]["api_equivalent"]
        print(
            f"{index:2d} at={at:%H:%M:%S} ci={counters.values['computed_input']:5d} "
            f"amount={cost['amount']} priced={cost['expected_hours']and cost['priced_hours']}"
            f"/{cost['expected_hours']} ratio={cost['cache_hit_ratio']}"
            f" tokens={cost['tokens']}"
        )
        at = at + timedelta(seconds=5)


main()
