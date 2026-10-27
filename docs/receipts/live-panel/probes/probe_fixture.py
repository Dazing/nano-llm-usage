"""Throwaway probe: fixture live fields only appear when set; advancer ticks; size receipt."""
import json, sys, time, urllib.request
sys.path.insert(0, "tests/fixtures")
from fixture_server import FixtureServer

def get(url):
    with urllib.request.urlopen(url, timeout=2) as r:
        return r.read().decode()

with FixtureServer() as fx:
    base = fx.state.base_url if hasattr(fx.state, "base_url") else fx.base_url
    print("DEFAULT /stats  :", get(f"{base}/stats"))
    print("DEFAULT /metrics:")
    print(get(f"{base}/metrics"))
    fx.state.set_live(running=3, waiting=1, prefill_tps=1240.4, decode_tps=180.2, routed=1000, routed_res=930)
    fx.state.set_draft({"model_name": "fixture-model", "engine": "0"}, 1000, 700)
    print("LIVE /stats     :", get(f"{base}/stats"))
    print("LIVE /metrics   :", get(f"{base}/metrics").strip().splitlines()[-4:])
    fx.state.stop_live_advancer()
    fx.state.set_live(running=None)  # clears the pair/live fields
    print("CLEARED /stats  :", get(f"{base}/stats"))

with FixtureServer() as fx:
    base = fx.base_url
    labels = {"model_name": "fixture-model", "engine": "0"}
    fx.state.uptime_origin = fx.state.clock()
    fx.state.start_live_advancer(labels=labels)
    for tick in (0, 1, 6):
        if tick:
            time.sleep(tick - (1 if tick == 6 else 0) if tick == 1 else 5)
        body = json.loads(get(f"{base}/stats"))
        metrics = get(f"{base}/metrics")
        draft = [line for line in metrics.splitlines() if "draft" in line and not line.startswith("#")]
        print(f"tick t={time.monotonic():.1f} stats={ {k: v for k, v in body.items() if k != 'model'} } draft={draft}")
    fx.state.stop_live_advancer()
    after = json.loads(get(f"{base}/stats"))["experts"]
    time.sleep(2.0)
    print("after stop, experts frozen:", after == json.loads(get(f"{base}/stats"))["experts"])

with FixtureServer() as fx:
    base = fx.base_url
    receipt = fx.state.use_representative_size()
    stats = get(f"{base}/stats")
    metrics = get(f"{base}/metrics")
    print("RECEIPT", receipt)
    print("served stats bytes", len(stats.encode()), "metrics bytes", len(metrics.encode()))
    doc = json.loads(stats)
    print("layer_units", len(doc["experts"]["layer_units"]), "x", len(doc["experts"]["layer_units"][0]),
          "| histogram lines", sum(1 for line in metrics.splitlines() if line.endswith("_bucket{le=\"10\"} ") is False and "_bucket" in line))
