"""Disposable: live-poller-only hit rate against the fixture (no collector)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "tests/fixtures")

from fixture_server import FixtureServer  # noqa: E402
from jarvis_dashboard.config import Config  # noqa: E402
from jarvis_dashboard.live import LiveMonitor  # noqa: E402

LABELS = {"model_name": "fixture-model", "engine": "0"}
server = FixtureServer(model="fixture-model").start()
st = server.state
st.set_live(running=3, waiting=1, prefill_tps=1240.4, decode_tps=180.2, routed=0, routed_res=0)
st.set_draft(LABELS, 0, 0)
st.start_live_advancer(labels=LABELS)

config = Config(
    radiance_base_url=server.base_url,
    source_id="live-receipt",
    poll_interval_seconds=15,
    http_timeout_seconds=3.0,
    database_path="/tmp/live-only-cadence.sqlite3",
    raw_retention_days=30,
    port=8099,
    log_level="WARNING",
)
monitor = LiveMonitor(config)
st.requests.clear()
monitor.start()
t0 = time.monotonic()
time.sleep(20.0)
window = time.monotonic() - t0
monitor.request_stop()
assert monitor.join(2.0)
stats_hits = st.requests.get("/stats", 0)
metrics_hits = st.requests.get("/metrics", 0)
snapshot = monitor.snapshot()
st.stop_live_advancer()
server.stop()

print(f"live_only_window_seconds={window:.2f}")
print(f"live_stats_hits={stats_hits} ({stats_hits / window:.2f}/s)")
print(f"live_metrics_hits={metrics_hits} ({metrics_hits / window:.2f}/s)")
print(f"joined_cleanly=True state={snapshot['state']} requests={snapshot['requests']}")
print(f"mtp={snapshot['mtp_acceptance']} moe={snapshot['moe_gpu_hit']}")
