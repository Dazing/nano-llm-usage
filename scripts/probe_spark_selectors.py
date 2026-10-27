"""Disposable probe: dump the live-panel sparkline/ring DOM the smoke checks rely on.

Run: PYTHONPATH=src .venv/bin/python scripts/probe_spark_selectors.py
Boots the same fixture + serve stack as scripts/smoke_frontend.py, opens one page, and
prints selector counts plus the outer HTML of the elements the new AC4/AC6/AC11 checks read.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_frontend as sf  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-probe-"))
    server = sf.FixtureServer()
    server.start()
    sf.reset_state(server)
    sf.install_fixture(server.state, sf.L, 1000, 100)
    sf.UptimeTicker(server)
    serve = sf.start_stack(server, tmp / "probe.sqlite3")
    browser = sf.Browser.launch(tmp / "chrome", [])
    page = browser.new_page(*sf.DESKTOP)
    page.add_script_on_new_document(sf.PROBE)
    try:
        page.navigate(f"http://127.0.0.1:{serve.port}/")
        sf.expect(page, lambda: sf.count_of(page, '[data-testid="live-panel"]') == 1,
                  "live panel", 60)
        server.state.start_live_advancer(labels=sf.L)
        sf.expect(page, lambda: sf.text_of(page, "live-requests") == "3 / 1", "live requests", 60)
        selectors = [
            '[data-testid="live-panel"]',
            '[data-testid="live-panel"] .live-left',
            '[data-testid="live-panel"] .live-left .spark',
            '[data-testid="live-panel"] .live-left .spark svg',
            '[data-testid="live-panel"] .gauge .spark svg',
            '[data-testid="live-panel"] .gauge .g-unit',
            '[data-testid="live-panel"] .live-cell',
            '[data-testid="live-panel"] .live-hint',
            '[data-testid="live-panel"] .rings-svg .track',
            '[data-testid="live-panel"] .live-right .rings-svg .track',
        ]
        for sel in selectors:
            print(f"COUNT {sel!r} = {sf.count_of(page, sel)}")
        print("MTP SPARK HTML:", page.evaluate(
            "(() => { const el = document.querySelector('[data-testid=\"live-panel\"] .live-left .spark');"
            " return el ? el.outerHTML.slice(0, 240) : 'MISSING'; })()"))
        print("GAUGE SPARK HTML:", page.evaluate(
            "(() => { const el = document.querySelector('[data-testid=\"live-panel\"] .gauge .spark');"
            " return el ? el.outerHTML.slice(0, 240) : 'MISSING'; })()"))
        print("RINGS HTML:", page.evaluate(
            "(() => { const el = document.querySelector('[data-testid=\"live-panel\"] .rings-svg');"
            " return el ? el.outerHTML.slice(0, 400) : 'MISSING'; })()"))
        print("RADII mtp:", sf.ring_radii(page, '[data-testid="live-panel"] .live-left'))
        print("RADII moe:", sf.ring_radii(page, '[data-testid="live-panel"] .live-right'))
        print("INSETS:", sf.ring_row_insets(page))
        print("SPARK MATCH:", page.evaluate(
            "(() => { const gauge = document.querySelector('[data-testid=\"live-panel\"]"
            " .gauge .spark svg');"
            " const mtp = document.querySelector('[data-testid=\"live-panel\"] .live-left .spark svg');"
            " const g = gauge.getBoundingClientRect(), m = mtp.getBoundingClientRect();"
            " return gauge.getAttribute('viewBox') === mtp.getAttribute('viewBox')"
            " && Math.abs(g.height - 30) <= 1 && Math.abs(g.height - m.height) <= 1"
            " && g.width > 120 && g.width > g.height * 3; })()"))
        print("GAUGE SVG CLASSES:", page.evaluate(
            "(() => [...document.querySelectorAll('[data-testid=\"live-panel\"] .gauge svg')]"
            ".map((el) => [el.getAttribute('class'), el.getAttribute('viewBox'),"
            " Math.round(el.getBoundingClientRect().width),"
            " Math.round(el.getBoundingClientRect().height)]))()"))
        return 0
    finally:
        browser.close()
        sf.stop_proc(serve) if hasattr(sf, "stop_proc") else serve.stop()
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
