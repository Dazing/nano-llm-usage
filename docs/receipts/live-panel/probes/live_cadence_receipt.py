"""Disposable Step 2 receipt: real `serve` against the fixture at 1 Hz.

Records fixture /stats and /metrics hit counts over a measured window, the
/api/v1/live body, and the SIGTERM shutdown wall time. Run from the repo root
with PYTHONPATH=src.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "scripts")
sys.path.insert(0, "tests/fixtures")

import smoke  # noqa: E402
from fixture_server import FixtureServer  # noqa: E402

LABELS = {"model_name": "fixture-model", "engine": "0"}

server = FixtureServer(model="fixture-model").start()
st = server.state
st.set_live(running=3, waiting=1, prefill_tps=1240.4, decode_tps=180.2, routed=0, routed_res=0)
st.set_draft(LABELS, 0, 0)
st.start_live_advancer(labels=LABELS)

proc = smoke.ServeProc(Path("/tmp/live-cadence.sqlite3"), server.base_url, smoke.free_port())
proc.start()
assert smoke.wait_for(lambda: smoke.raw_get(proc.port, "/health/ready")[0] == 200, timeout=40), "serve never became ready"

st.requests.clear()
t0 = time.monotonic()
time.sleep(10.0)
window = time.monotonic() - t0
stats_hits = st.requests.get("/stats", 0)
metrics_hits = st.requests.get("/metrics", 0)

status, live, _headers = smoke.api(proc.port, "/live")
ready_status, ready_ct = smoke.raw_get(proc.port, "/health/ready")

t_kill = time.monotonic()
proc.stop()
exit_code = proc.proc.returncode
shutdown = time.monotonic() - t_kill

st.stop_live_advancer()
server.stop()

print(f"window_seconds={window:.2f}")
print(f"fixture_stats_hits={stats_hits} ({stats_hits / window:.2f}/s)")
print(f"fixture_metrics_hits={metrics_hits} ({metrics_hits / window:.2f}/s)")
print(f"GET /api/v1/live -> {status} {live}")
print(f"GET /health/ready -> {ready_status} {ready_ct}")
print(f"sigterm_exit_code={exit_code} shutdown_seconds={shutdown:.2f}")
