"""Disposable probe: sample the API cost well while the summary response is delayed.

Run: PYTHONPATH=src .venv/bin/python scripts/probe_cost_well_delay.py
Reproduces the smoke's cost_estimate entry (publish, delay /api/v1/summary, navigate) and
prints the well's state every 250 ms so the "no data" window is visible.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_frontend as sf  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-probe-cost-"))
    server = sf.FixtureServer()
    server.start()
    sf.reset_state(server)
    sf.install_fixture(server.state, sf.L, 1000, 100)
    ticker = sf.UptimeTicker(server)  # noqa: F841 (keep the keeper alive)
    serve = sf.start_stack(server, tmp / "cost.sqlite3", rates=sf.RATE_ENV)
    browser = sf.Browser.launch(tmp / "chrome", [])
    page = browser.new_page(*sf.DESKTOP)
    page.add_script_on_new_document(sf.PROBE)
    try:
        page.navigate(f"http://127.0.0.1:{serve.port}/")
        sf.expect(page, lambda: sf.count_of(page, '[data-testid="range-24h"]') == 1,
                  "dashboard", 60)
        sf.publish(server, 1905, 162, 100, 100, 600_000)
        time.sleep(sf.TWO_POLLS)
        sf.publish(server, 2000, 170, 1_000_100, 100_100, 1_200_000)
        time.sleep(sf.TWO_POLLS)
        sf.expect(page, lambda: (sf.text_of(page, "cost-today") or "").startswith("≈ $"),
                  "priced well", 60)
        print("priced well:", sf.text_of(page, "cost-today"))
        sf.arm_delay(page, 3500)
        page.navigate(f"http://127.0.0.1:{serve.port}/")
        started = time.monotonic()
        while time.monotonic() - started < 9:
            row = page.evaluate(
                "(() => { const t = (id) => { const el = document.querySelector("
                "`[data-testid=${id}]`); return el ? el.textContent.trim() : 'ABSENT'; };"
                " return {today: t('cost-today'), period: t('cost-period'),"
                " unpriced: t('cost-unpriced'), panel: t('cost-panel').slice(0, 60)}; })()"
            )
            print(f"t={time.monotonic() - started:5.2f}s {row}")
            time.sleep(0.25)
        return 0
    finally:
        browser.close()
        serve.stop()
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
