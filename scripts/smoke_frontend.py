#!/usr/bin/env python
"""Delivery 3 frontend smoke: built bundle, real ``serve`` process, real Chromium.

Follows ``scripts/smoke.py`` conventions: one printed receipt per observation,
non-zero exit on the first failed check. The browser is driven through
``scripts/browser_driver.py`` (stdlib CDP) against the fixture collector.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
for _extra in (ROOT / "tests" / "fixtures", ROOT / "src", ROOT / "scripts"):
    sys.path.insert(0, str(_extra))

from browser_driver import Browser  # noqa: E402
from fixture_server import FixtureServer, install_fixture  # noqa: E402
from smoke import (  # noqa: E402
    L,
    POLL,
    TWO_POLLS,
    ServeProc,
    UptimeTicker,
    api,
    check,
    free_port,
    get_path,
    raw_get,
    receipt,
    reset_state,
    set_counts,
    stop_all,
    wait_for,
)

STATIC = ROOT / "src" / "jarvis_dashboard" / "static"
SHOTS = ROOT / "docs" / "receipts" / "delivery-3"
LIVE_SHOTS = ROOT / "docs" / "receipts" / "live-panel"
SECOND = {"model_name": "second-model", "engine": "0"}
FOCUS_EXPECTED = ["theme-toggle", "range-24h", "range-7d", "range-30d", "range-custom",
                  "chart-disclosure"]
DESKTOP = (1440, 900)
MOBILE = (390, 844)

# Installed before every document: counts poll timers, records fetch URLs, and
# can delay a response for URLs containing ``delayMatch`` (selection race).
PROBE = """
(() => {
  const p = {timers: [], cleared: [], fetches: [], delayMs: 0, delayMatch: ""};
  const si = window.setInterval, ci = window.clearInterval, f = window.fetch;
  window.setInterval = (fn, ms, ...a) => { const id = si(fn, ms, ...a); p.timers.push({id, ms}); return id; };
  window.clearInterval = (id) => { p.cleared.push(id); return ci(id); };
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    p.fetches.push(url);
    const res = await f(input, init);
    if (p.delayMs && url.includes(p.delayMatch)) {
      const body = await res.arrayBuffer();
      await new Promise((r) => setTimeout(r, p.delayMs));
      return new Response(body, {status: res.status, statusText: res.statusText,
                                 headers: res.headers});
    }
    return res;
  };
  window.__probe = p;
})();
"""

START = time.monotonic()


# -- small DOM helpers ---------------------------------------------------- #
def js(selector: str) -> str:
    return json.dumps(f'[data-testid="{selector}"]')


def text_of(page, testid: str):
    return page.evaluate(
        f"(() => {{ const el = document.querySelector({js(testid)});"
        " return el ? el.textContent.trim() : null; })()"
    )


def visible(page, testid: str) -> bool:
    return page.evaluate(
        f"(() => {{ const el = document.querySelector({js(testid)});"
        " return el ? el.offsetParent !== null : false; })()"
    )


def count_of(page, selector: str) -> int:
    return page.evaluate(f"document.querySelectorAll({json.dumps(selector)}).length")


def probe(page, expression: str):
    return page.evaluate(f"(() => {{ const p = window.__probe; return {expression}; }})()")


def fetches(page, needle: str = "") -> list[str]:
    return page.evaluate(
        f"(() => window.__probe.fetches.filter((u) => u.includes({json.dumps(needle)})))()"
    )


def last_fetch(page, needle: str = ""):
    urls = fetches(page, needle)
    return urls[-1] if urls else None


def expect(page, predicate, what: str, timeout: float = 45.0) -> None:
    """Poll a page condition; on failure dump the page state that surrounded it."""
    started = time.monotonic()
    if wait_for(predicate, timeout):
        receipt(f"wait[{what}]", f"ok in {time.monotonic() - started:.1f}s")
        return
    receipt("context_elapsed", round(time.monotonic() - started, 1))
    receipt("context_fetches", fetches(page, "/api/v1/")[-8:])
    receipt("context_pressed", page.evaluate(
        "(() => ['range-24h','range-7d','range-30d'].map((t) => t + '=' +"
        " (document.querySelector(`[data-testid=${t}]`) || {}).ariaPressed).join(' '))()"))
    receipt("context_badges", [text_of(page, "status-badge"), text_of(page, "last-update"),
                               text_of(page, "range-error"), text_of(page, "api-error")])
    receipt("context_body", (page.evaluate("document.body.innerText") or "")[:700])
    page.screenshot(SHOTS / "failure-context.png")
    raise AssertionError(f"{what} did not become true within {timeout:.0f}s")


def set_date(page, testid: str, value: str) -> None:
    page.evaluate(
        f"(() => {{ const el = document.querySelector({js(testid)}); el.value = {json.dumps(value)};"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " el.dispatchEvent(new Event('change', {bubbles: true})); })()"
    )


def open_details(page, testid: str) -> None:
    page.evaluate(
        f"(() => {{ const el = document.querySelector({js(testid)});"
        " if (el && !el.open) el.open = true; })()"
    )


def shot(page, name: str) -> None:
    path = page.screenshot(SHOTS / name)
    receipt(f"screenshot {name}", f"{path} ({path.stat().st_size} bytes)")


def live_shot(page, name: str) -> None:
    path = page.screenshot(LIVE_SHOTS / name)
    receipt(f"screenshot {name}", f"{path} ({path.stat().st_size} bytes)")


def day(offset: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=offset)).strftime("%Y-%m-%d")


def dom_number(value) -> str:
    return "no data" if value is None else f"{int(value):,}"


def summary_of(port: int, series_id: str) -> dict:
    status, body, _ = api(port, "/summary", f"series_id={series_id}")
    return body if status == 200 else {}


def series_items(port: int) -> list[dict]:
    status, body, _ = api(port, "/series")
    return body.get("items", []) if status == 200 else []


def series_ids(port: int) -> list[str]:
    return [item["series_id"] for item in series_items(port)]


# -- phases ---------------------------------------------------------------- #
def preflight() -> None:
    print("--- phase 1: preflight")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/export_openapi.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    check("openapi snapshot current", proc.returncode, 0)
    check("frontend build present", (STATIC / "index.html").is_file(), True)
    check("chromium present", shutil.which("chromium") is not None, True)


def start_stack(server: FixtureServer, db: Path, source: str = "smoke") -> ServeProc:
    port = free_port()
    serve = ServeProc(db, server.base_url, port)
    serve.env["SOURCE_ID"] = source
    serve.env["STATIC_DIR"] = str(STATIC)
    serve.start()
    ok = wait_for(lambda: get_path(port, "/health/ready")[0] == 200, 30)
    check(f"serve ready on port {port}", ok, True)
    return serve


def publish(server: FixtureServer, ci, go, ri=None, ro=None) -> None:
    """Publish one metric set and keep republishing it every 20 s.

    `smoke.set_counts` chains its own keeper timers, and several chains flapping on the
    same series make state phases (request counters absent, collection gaps) unpredictable,
    so this cancels every running timer and owns the single publisher from here on.
    """
    for thread in threading.enumerate():
        if isinstance(thread, threading.Timer):
            thread.cancel()

    def apply() -> None:
        st = server.state
        st.uptime_s = 100.0 + (time.monotonic() - START)
        st.drop_series(L)
        install_fixture(st, L, str(ci), str(go),
                        None if ri is None else str(ri), None if ro is None else str(ro))

    def keeper() -> None:
        apply()
        timer = threading.Timer(20.0, keeper)
        timer.daemon = True
        timer.start()

    apply()
    timer = threading.Timer(20.0, keeper)
    timer.daemon = True
    timer.start()


def accounting(server: FixtureServer, port: int) -> str:
    print("--- phase 3: A2 accounting through the API")
    publish(server, 1600, 140, 1000, 100)
    time.sleep(TWO_POLLS)
    publish(server, 1900, 160, 1000, 100)

    def lifetime():
        ids = series_ids(port)
        if not ids:
            return None
        life = summary_of(port, ids[0]).get("ranges", {}).get("lifetime")
        return life if life and life.get("processed_total") == "960" else None

    assert wait_for(lambda: lifetime() is not None, 60), "A2 chain never reached 960 over HTTP"
    life = lifetime()
    check("api_processed_total", life["processed_total"], "960")
    check("api_lifetime_input", life["counters"]["computed_input"]["tokens"], "900")
    check("api_lifetime_output", life["counters"]["generated_output"]["tokens"], "60")
    series_id = series_ids(port)[0]
    receipt("api_series_id", series_id[:12] + "…")
    return series_id


def dom_basics(page) -> None:
    print("--- phase 4: built bundle renders the API values")
    started = time.monotonic()
    ok = wait_for(lambda: text_of(page, "lifetime-total") == "960", 40)
    receipt("dom_first_paint_seconds", round(time.monotonic() - started, 1))
    check("dom_renders_lifetime_total", ok, True)
    check("product name", text_of(page, "product-name"), "Jarvis Token Dashboard")
    check("dom_lifetime_total", text_of(page, "lifetime-total"), "960")
    check("dom_lifetime_input", text_of(page, "lifetime-input"), "900")
    check("dom_lifetime_output", text_of(page, "lifetime-output"), "60")
    receipt("dom_lifetime_start", text_of(page, "lifetime-start"))
    check("dom_lifetime_start_rendered",
          (text_of(page, "lifetime-start") or "").startswith("Collecting since"), True)
    check("dom_utc_labelled", page.evaluate(
        "(() => { const cards = [...document.querySelectorAll('[data-testid^=\"card-\"]')];"
        " return cards.length === 3 && cards.every((c) => c.textContent.includes('UTC'));"
        " })()"
    ), True)
    receipt("dom_series_name", text_of(page, "series-name"))
    receipt("dom_status_badge", text_of(page, "status-badge"))
    receipt("dom_last_update", text_of(page, "last-update"))
    shot(page, "desktop-1440-normal.png")


def ranges(page) -> None:
    print("--- phase 5: range presets and the exclusive custom end")
    page.click('[data-testid="range-24h"]')
    expect(page, lambda: last_fetch(page, "/api/v1/history") is not None, "history-after-24h")
    check("range_24h_bucket", parse_qs(urlparse(last_fetch(page, "/api/v1/history")).query)["bucket"][0],
          "hour")
    page.click('[data-testid="range-7d"]')
    expect(page, lambda: "bucket=day" in (last_fetch(page, "/api/v1/history") or ""), "day-bucket-7d")
    page.click('[data-testid="range-30d"]')
    expect(page, lambda: (parse_qs(urlparse(last_fetch(page, "/api/v1/history")).query)["from"][0]
                          .endswith("T00:00:00.000Z")), "midnight-aligned-30d")
    check("range_30d_bucket", parse_qs(urlparse(last_fetch(page, "/api/v1/history")).query)["bucket"][0],
          "day")

    open_details(page, "range-custom")
    check("custom_end_label", page.evaluate(
        "document.body.innerText.includes('UTC end (exclusive)')"), True)
    set_date(page, "custom-from", day(-40))
    set_date(page, "custom-to", day(0))
    before = len(fetches(page, "/api/v1/history"))
    page.click('[data-testid="custom-apply"]')
    expect(page, lambda: len(fetches(page, "/api/v1/history")) > before, "custom-range-fetch")
    query = parse_qs(urlparse(last_fetch(page, "/api/v1/history")).query)
    check("custom_bucket", query["bucket"][0], "day")
    check("custom_from_midnight", query["from"][0].endswith("T00:00:00.000Z"), True)
    check("custom_end_exclusive", query["to"][0], f"{day(1)}T00:00:00.000Z")
    shot(page, "desktop-1440-custom-range.png")

    set_date(page, "custom-from", day(-400))
    set_date(page, "custom-to", day(0))
    before = len(fetches(page, "/api/v1/history"))
    page.click('[data-testid="custom-apply"]')
    time.sleep(2)
    check("custom_over_limit_blocked", len(fetches(page, "/api/v1/history")), before)
    check("range_error_names_the_limit",
          "366" in (text_of(page, "range-error") or ""), True)
    page.click('[data-testid="range-24h"]')
    expect(page, lambda: (parse_qs(urlparse(last_fetch(page, "/api/v1/history")).query)["bucket"][0]
                          == "hour"), "back-to-hour-buckets")


def chart_integrity(page, server: FixtureServer) -> None:
    print("--- phase 6: null buckets, one canvas, incomplete bucket note")
    publish(server, 1905, 162, 100, 100)
    server.state.drop_series(L)  # fixture-side collection gap
    time.sleep(POLL + 2)
    publish(server, 1910, 164, 100, 100)
    time.sleep(TWO_POLLS)
    page.click('[data-testid="range-24h"]')
    ok = wait_for(lambda: count_of(page, '[data-testid="chart-table"] tbody tr') > 0, 45)
    check("chart_table_rows_rendered", ok, True)
    open_details(page, "chart-disclosure")
    row = page.evaluate(
        "(() => { const rows = [...document.querySelectorAll('[data-testid=\"chart-table\"] tbody tr')];"
        " for (const r of rows) { const cells = [...r.querySelectorAll('td')].map((c) => c.textContent.trim());"
        " if (cells.some((c) => c === 'no data')) return cells; } return null; })()"
    )
    assert row is not None, "no chart-table row with a null bucket"
    check("null_bucket_label", "no data" in row, True)
    check("null_bucket_never_zero", any(c == "0" for c in row), False)
    receipt("null_bucket_row", " | ".join(row))
    check("canvas_count", count_of(page, '[data-testid="chart-canvas"]'), 1)
    check("incomplete_note", visible(page, "incomplete-note"), True)
    shot(page, "desktop-1440-chart-table.png")
    open_details(page, "diagnostics-disclosure")
    check("diagnostics_populated",
          page.evaluate(
              "(() => { const d = document.querySelector('[data-testid=\"diagnostics-disclosure\"]');"
              " return d.innerText.includes('unobserved for') || d.innerText.includes('No collection'); })()"
          ), True)
    shot(page, "desktop-1440-diagnostics.png")
    open_details(page, "chart-disclosure")


def empty_and_baseline(tmp: Path, browser: Browser):
    print("--- phase 7a/7b: empty database and baseline-only series")
    server = FixtureServer()
    server.start()
    reset_state(server)
    page = browser.new_page(*DESKTOP)
    page.add_script_on_new_document(PROBE)
    try:
        serve = start_stack(server, tmp / "empty.sqlite3")
        page.navigate(f"http://127.0.0.1:{serve.port}/")
        ok = wait_for(lambda: visible(page, "empty-state"), 40)
        check("state_empty", ok, True)
        page.set_viewport(*MOBILE)
        shot(page, "mobile-390-empty.png")
        page.set_viewport(*DESKTOP)

        UptimeTicker(server)
        install_fixture(server.state, L, 5000, 500)
        ok = wait_for(lambda: len(series_ids(serve.port)) >= 1, 60)
        check("baseline_series_collected", ok, True)
        # Freeze the series at its baseline epoch: without further observations the
        # lifetime has no accepted interval, so totals stay "no data" and cannot drift to 0.
        server.state.drop_series(L)
        expect(page, lambda: visible(page, "baseline-note"), "baseline-note", 60)
        check("state_baseline", visible(page, "baseline-note"), True)
        check("baseline_total_is_not_zero", text_of(page, "lifetime-total"), "no data")
        # This series never published the optional request-accounted counters, so the
        # request panel must say "unavailable" per key instead of borrowing an engine number.
        open_details(page, "requests-disclosure")
        notes = '[data-testid="unavailable-note"]'
        expect(page, lambda: count_of(page, notes) >= 2, "two unavailable notes", 60)
        check("state_optional_unavailable", count_of(page, notes), 2)
        serve.stop()
    finally:
        server.stop()
        browser.command("Target.closeTarget", {"targetId": page.target_id})


def offline(page, server: FixtureServer) -> None:
    print("--- phase 7c: fixture disconnect keeps history, restores cleanly")
    before = text_of(page, "last-update")
    # The chart-integrity phase advanced the counters, so the expected total is whatever the
    # page shows right now: outage must not change it, whatever it is.
    kept = text_of(page, "lifetime-total")
    server.state.metrics_status = 500
    started = time.monotonic()
    ok = wait_for(lambda: visible(page, "offline-banner"), 45)
    receipt("offline_seconds", round(time.monotonic() - started, 1))
    check("offline_banner", ok, True)
    check("state_offline_history_kept", text_of(page, "lifetime-total"), kept)
    shot(page, "desktop-1440-offline.png")
    server.state.metrics_status = None
    ok = wait_for(lambda: (text_of(page, "status-badge") or "") in ("Healthy", "Degraded")
                  and (text_of(page, "last-update") or "") != before, 45)
    receipt("recovered_badge", text_of(page, "status-badge"))
    check("offline_recovers", ok, True)


def api_failure(page, serve: ServeProc) -> None:
    print("--- phase 7d: API failure shows the stale notice, restart recovers")
    assert serve.proc is not None
    kept = text_of(page, "lifetime-total")
    serve.proc.kill()
    serve.proc.wait(timeout=10)
    ok = wait_for(lambda: visible(page, "api-error") and visible(page, "stale-notice"), 45)
    check("state_api_failure_stale", ok, True)
    check("api_failure_history_kept", text_of(page, "lifetime-total"), kept)
    page.set_viewport(*MOBILE)
    shot(page, "mobile-390-stale.png")
    page.set_viewport(*DESKTOP)
    serve.start()
    ok = wait_for(lambda: not visible(page, "api-error"), 60)
    check("api_failure_recovers", ok, True)


def optional_unavailable(page, server: FixtureServer) -> None:
    print("--- phase 7e: request-accounted counters absent -> unavailable")
    publish(server, 1905, 162)
    open_details(page, "requests-disclosure")
    # The counters stopped being published mid-life: the accumulated request history stays,
    # it is not rewritten into "unavailable", and the engine total stays out of this panel.
    check("requests_history_kept_when_absent",
          count_of(page, '[data-testid="unavailable-note"]'), 0)
    total = text_of(page, "lifetime-total") or ""
    summary_text = text_of(page, "requests-summary") or ""
    check("requests_panel_has_no_engine_total",
          re.search(rf"(?<![\d.,]){re.escape(total)}(?![\d.,])", summary_text) is not None, False)


def live_phase(page, server: FixtureServer, ticker: UptimeTicker) -> UptimeTicker:
    """The live row, plan AC1-AC6, against a fixture that advances once a second.

    The advancer adds 100 draft tokens per second at a 0.7 accept ratio and 100
    routed experts per second at a 0.93 resident ratio, so every ratio the panel
    prints is a number this file chose. Windows fill on the wall clock: the 5 s
    slot after ~6 s, the 1 m slot after ~62 s; the 30 m slot is proven by the
    injected-clock tests, not by a 30-minute browser wait.
    """
    print("--- phase 7f: the live row follows the fixture at 1 Hz")
    started = time.monotonic()
    check("live_panel_visible", visible(page, "live-panel"), True)
    check("live_tiles", count_of(page, '[data-testid="live-panel"] .live-cell'), 5)
    # No draft counters, no experts block and no running requests yet: five placeholders.
    check("live_mtp_placeholder_before_counters", text_of(page, "live-mtp"), "- / - / - / -")
    check("live_moe_placeholder_before_experts", text_of(page, "live-moe"), "- / - / -")

    server.state.start_live_advancer(labels=L)
    expect(page, lambda: text_of(page, "live-requests") == "3 / 1", "live AC1 requests")
    check("live_state_ac1", text_of(page, "live-state"), "Live")
    check("live_prefill_ac1", text_of(page, "live-prefill"), "1,240 tok/s")
    check("live_decode_ac1", text_of(page, "live-decode"), "180 tok/s · 60/req")
    # Lifetime needs a second /metrics sample, so it lands about a second after the
    # instantaneous stats tiles - a dash here is the engine refusing a one-point ratio.
    expect(page, lambda: (text_of(page, "live-mtp") or "").endswith("70%"), "live AC1 MTP lifetime", 20)
    mtp_slots = (text_of(page, "live-mtp") or "").split(" / ")
    check("live_mtp_lifetime_ac1", mtp_slots[3], "70%")
    check("live_mtp_windows_unfilled_ac1", mtp_slots[2], "-")
    live_shot(page, "live-1440-ac1.png")

    # Zero running requests: the per-request slot is undefined, so it is a dash -
    # and the instantaneous throughput beside it stays real (plan AC1).
    server.state.set_live(running=0, waiting=1, prefill_tps=1240.4, decode_tps=180.2)
    expect(page, lambda: (text_of(page, "live-decode") or "").endswith("· -"),
           "live per-request placeholder")
    check("live_requests_zero_running", text_of(page, "live-requests"), "0 / 1")
    check("live_decode_value_kept", (text_of(page, "live-decode") or "").startswith("180 tok/s"), True)
    server.state.set_live(running=3, waiting=1, prefill_tps=1240.4, decode_tps=180.2)

    expect(page, lambda: (text_of(page, "live-mtp") or "").startswith("70% / - / - /"),
           "live MTP 5 s window", 30)
    # MoE lags MTP by a poll here: the two `running` edits above clear the experts block
    # for a second each, so its baseline point is slightly younger.
    expect(page, lambda: (text_of(page, "live-moe") or "").startswith("93% / - / -"),
           "live MoE 5 s window", 30)
    receipt("live_5s_window_seconds", round(time.monotonic() - started, 1))
    live_shot(page, "live-1440-window-5s.png")

    expect(page, lambda: (text_of(page, "live-mtp") or "").startswith("70% / 70% / - /"),
           "live MTP 1 m window", 100)
    expect(page, lambda: (text_of(page, "live-moe") or "").startswith("93% / 93% / -"),
           "live MoE 1 m window", 100)
    receipt("live_1m_window_seconds", round(time.monotonic() - started, 1))
    live_shot(page, "live-1440-window-1m.png")

    # A Radiance restart is an uptime decrease. The stats run resets, so the MoE
    # windows - whose baseline lives in /stats - go blank and refill, while MTP,
    # measured from /metrics, keeps its windows (plan AC3).
    ticker.stop()
    server.state.uptime_s = 5.0
    expect(page, lambda: text_of(page, "live-moe") == "- / - / -", "live MoE windows reset", 20)
    check("live_mtp_keeps_windows_across_stats_reset",
          (text_of(page, "live-mtp") or "").startswith("70% / 70%"), True)
    check("live_instant_tile_survives_stats_reset", text_of(page, "live-prefill"), "1,240 tok/s")
    live_shot(page, "live-1440-run-reset.png")
    expect(page, lambda: (text_of(page, "live-moe") or "").startswith("93%"),
           "live MoE windows refilled", 30)
    ticker = UptimeTicker(server, base=200.0)  # resumes the chainable clock above the drop

    # Both endpoints failing: every tile a dash, the chip stale, no 15 s panel damage.
    server.state.stats_fail = True
    server.state.metrics_status = 500
    expect(page, lambda: text_of(page, "live-state") == "Stale", "live outage stale chip", 20)
    check("live_outage_requests", text_of(page, "live-requests"), "- / -")
    check("live_outage_prefill", text_of(page, "live-prefill"), "-")
    check("live_outage_decode", text_of(page, "live-decode"), "- · -")
    check("live_outage_mtp", text_of(page, "live-mtp"), "- / - / - / -")
    check("live_outage_moe", text_of(page, "live-moe"), "- / - / -")
    receipt("live_outage_seconds", round(time.monotonic() - started, 1))
    live_shot(page, "live-1440-stale.png")

    server.state.stats_fail = False
    server.state.metrics_status = None
    expect(page, lambda: text_of(page, "live-state") == "Live", "live outage recovers", 20)
    check("live_requests_after_outage", text_of(page, "live-requests"), "3 / 1")

    # No experts block at all (a non-MoE model): MoE reads as absent, MTP is untouched.
    server.state.suppress_experts = True
    expect(page, lambda: text_of(page, "live-moe") == "- / - / -", "live MoE absent", 20)
    check("live_mtp_unaffected_by_absent_moe", (text_of(page, "live-mtp") or "").endswith("70%"), True)
    check("live_state_with_moe_absent", text_of(page, "live-state"), "Live")
    server.state.suppress_experts = False

    page.set_viewport(*MOBILE)
    expect(page, lambda: page.evaluate(
        "(() => { const el = document.querySelector('[data-testid=\"live-panel\"]');"
        " return el.getBoundingClientRect().right <= window.innerWidth + 1; })()"),
        "live row fits 390 px", 10)
    receipt("live_390_body_scroll_width", page.evaluate("document.documentElement.scrollWidth"))
    live_shot(page, "live-390-mobile.png")
    page.set_viewport(*DESKTOP)
    server.state.stop_live_advancer()
    receipt("live_phase_seconds", round(time.monotonic() - started, 1))
    return ticker


def poll_lifecycle(page) -> None:
    print("--- phase 8: one 15 s poll timer and one 1 s live timer, both paused when hidden")
    intervals = probe(page, "p.timers.filter((t) => !p.cleared.includes(t.id)).map((t) => t.ms)")
    check("poll_timers", sorted(intervals), [1000, 15000])
    base = probe(page, "p.fetches.length")
    page.set_visibility(True)
    time.sleep(8)
    check("polls_paused_when_hidden", probe(page, "p.fetches.length"), base)
    check("poll_timers_cleared_when_hidden",
          probe(page, "p.timers.filter((t) => !p.cleared.includes(t.id)).length"), 0)
    page.set_visibility(False)
    page.tab(1)
    ok = wait_for(lambda: probe(page, "p.fetches.length") > base, 30)
    check("poll_resumes", ok, True)


def theme_and_a11y(page, port: int) -> None:
    print("--- phase 10: theme persistence, reduced motion, visible keyboard focus")
    page.evaluate("localStorage.removeItem('jarvis-theme')")
    page.navigate(f"http://127.0.0.1:{port}/")
    page.click('[data-testid="theme-toggle"]')
    check("theme_applied", page.evaluate("document.documentElement.dataset.theme"), "light")
    check("theme_persisted", page.evaluate("localStorage.getItem('jarvis-theme')"), "light")
    page.navigate(f"http://127.0.0.1:{port}/")
    check("theme_after_reload", page.evaluate("document.documentElement.dataset.theme"), "light")
    page.click('[data-testid="theme-toggle"]')
    check("theme_cycles_to_dark", page.evaluate("document.documentElement.dataset.theme"), "dark")
    shot(page, "desktop-1440-dark.png")
    page.set_media("prefers-reduced-motion", "reduce")
    check("reduced_motion", page.evaluate(
        "getComputedStyle(document.querySelector('[data-testid=\"theme-toggle\"]')).transitionDuration"
    ), "0s")
    page.set_media("prefers-reduced-motion", "no-preference")

    page.evaluate("document.activeElement && document.activeElement.blur()")
    seen = []
    for _ in range(len(FOCUS_EXPECTED)):
        page.tab(1)
        seen.append(page.evaluate(
            "(() => { const el = document.activeElement;"
            " const box = el && el.closest('[data-testid]');"
            " return {id: box ? box.getAttribute('data-testid') : null,"
            " outline: el ? getComputedStyle(el).outlineWidth : null}; })()"
        ))
    receipt("keyboard_focus_sequence", [item["id"] for item in seen])
    check("keyboard_focus_order", [item["id"] for item in seen], FOCUS_EXPECTED)
    check("keyboard_focus_visible", [item["outline"] for item in seen], ["2px"] * len(seen))


def selection_race(page, server: FixtureServer, port: int) -> None:
    print("--- phase 9: a delayed response for the old series cannot overwrite")
    install_fixture(server.state, SECOND, 500, 50)
    ok = wait_for(lambda: len(series_ids(port)) >= 2, 60)
    check("second_series_collected", ok, True)
    items = series_items(port)
    first = next(i["series_id"] for i in items if i["model_name"] == L["model_name"])
    second = next(i["series_id"] for i in items if i["model_name"] == SECOND["model_name"])
    # The header picker appears once the page's own poll sees the second series; reload so the
    # fresh mount fetches the list, and record how many series fetches the load made.
    page.navigate(f"http://127.0.0.1:{port}/")
    receipt("series_fetches_on_load",
            probe(page, "p.fetches.filter((u) => u.includes('/api/v1/series')).length"))
    expect(page, lambda: count_of(page, '[data-testid="series-picker"]') == 1, "series picker", 60)
    receipt("second_series_lifetime_total", summary_of(port, second).get("ranges", {})
            .get("lifetime", {}).get("processed_total"))
    ok = wait_for(lambda: page.evaluate(
        f"(() => [...document.querySelectorAll('[data-testid=\"series-option\"]')]"
        f".map((o) => o.value).includes({json.dumps(second)}))()"), 45)
    check("picker_lists_second_series", ok, True)
    page.evaluate(
        f"(() => {{ window.__probe.delayMatch = {json.dumps('series_id=' + first)};"
        " window.__probe.delayMs = 2000; })()"
    )
    # A fresh mount shows the skeleton until the first refresh lands; the chips come with it.
    expect(page, lambda: count_of(page, '[data-testid="range-7d"]') == 1, "range chips", 60)
    page.click('[data-testid="range-7d"]')  # in-flight requests for the first series
    time.sleep(0.2)
    page.evaluate(
        f"(() => {{ const el = document.querySelector('[data-testid=\"series-picker\"]');"
        f" el.value = {json.dumps(second)};"
        " el.dispatchEvent(new Event('change', {bubbles: true})); })()"
    )
    time.sleep(3.5)
    page.evaluate("window.__probe.delayMs = 0; window.__probe.delayMatch = '';")
    check("race_series_name", "second-model" in (text_of(page, "series-name") or ""), True)
    check("race_no_stale_overwrite", "first-model" not in (text_of(page, "card-lifetime") or ""), True)
    life = summary_of(port, second).get("ranges", {}).get("lifetime", {})
    check("race_lifetime_matches_selection",
          text_of(page, "lifetime-total"), dom_number(life.get("processed_total")))


def network(page, server: FixtureServer, port: int) -> None:
    print("--- phase 11: same-origin traffic only, clean console, fixture untouched")
    page.drain()
    allowed = f"http://127.0.0.1:{port}/"
    # Chromium renders <input type=date>'s calendar glyph as an inline data: URL; those are not
    # network requests. Everything that leaves the page must be same-origin.
    receipt("inline_data_urls", len([u for u in set(page.requests) if u.startswith("data:")]))
    check("browser_requests_local_only",
          [u for u in page.requests
           if not u.startswith(allowed) and not u.startswith("data:")][:3], [])
    # The client fetches same-origin relative paths; resolve them against the page origin.
    fetches = [u if "://" in u else allowed.rstrip("/") + u
               for u in probe(page, "p.fetches.slice()")]
    check("probe_fetches_local_only",
          [u for u in fetches if not u.startswith(allowed) and not u.startswith("data:")][:3], [])
    fixture_port = urlparse(server.base_url).port
    check("no_fixture_traffic",
          [u for u in page.requests + fetches if f":{fixture_port}" in u][:3], [])
    errors = [entry for entry in page.console if entry[0] == "console" and entry[1] == "error"]
    receipt("console_errors", len(errors))
    check("console_clean", errors[:3], [])
    exceptions = [entry for entry in page.console if entry[0] == "exception"]
    check("no_page_exceptions", exceptions[:3], [])
    receipt("browser_log_entries", [entry for entry in page.console if entry[0] == "log"][:3])

    paths = dict(server.state.requests)
    baseline = paths.get("/metrics", 0)
    stats_baseline = paths.get("/stats", 0)
    receipt("fixture_paths", sorted(paths))
    window = 2 * POLL + 2
    time.sleep(window)
    after = dict(server.state.requests)
    delta = after.get("/metrics", 0) - baseline
    stats_delta = after.get("/stats", 0) - stats_baseline
    receipt("fixture_requests_delta", delta)
    receipt("fixture_stats_delta", stats_delta)
    # Every path is now polled by the collector (every POLL s) *and* the live poller
    # (every second, plan D2); the browser adds nothing to the fixture (plan AC6).
    expected = window + window / POLL
    check("fixture_delta_is_collector_plus_live", abs(delta - expected) <= 3, True)
    check("fixture_stats_delta_is_collector_plus_live", abs(stats_delta - expected) <= 3, True)
    check("fixture_sees_no_browser_traffic",
          sorted(set(after) - {"/metrics", "/stats"}), [])


# -- runner ---------------------------------------------------------------- #
def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    LIVE_SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-frontend-smoke-"))
    receipt("workdir", tmp)
    server = FixtureServer()
    browser = None
    serve = None
    ticker = None
    try:
        preflight()
        print("--- phase 2: fixture collector and real serve process")
        server.start()
        reset_state(server)
        install_fixture(server.state, L, 1000, 100)
        ticker = UptimeTicker(server)
        serve = start_stack(server, tmp / "front.sqlite3")

        accounting(server, serve.port)

        browser = Browser.launch(tmp / "chrome", [])
        receipt("chromium_pid", browser.pid)
        page = browser.new_page(*DESKTOP)
        page.add_script_on_new_document(PROBE)
        page.navigate(f"http://127.0.0.1:{serve.port}/")
        root_status, root_type = raw_get(serve.port, "/")
        receipt("root_response", f"{root_status} {root_type.split(';')[0] or 'no content-type'}")

        dom_basics(page)
        ranges(page)
        chart_integrity(page, server)
        empty_and_baseline(tmp, browser)
        offline(page, server)
        api_failure(page, serve)
        optional_unavailable(page, server)
        ticker = live_phase(page, server, ticker)
        poll_lifecycle(page)
        theme_and_a11y(page, serve.port)
        selection_race(page, server, serve.port)
        network(page, server, serve.port)

        page.set_viewport(*MOBILE)
        shot(page, "mobile-390-normal.png")
        page.set_viewport(*DESKTOP)
        receipt("screenshots_written", len(list(SHOTS.glob("*.png"))))
        receipt("browser_requests", len(page.requests))
        return 0
    finally:
        if browser is not None:
            browser.close()
        if serve is not None and serve.proc is not None and serve.proc.poll() is None:
            serve.proc.terminate()
            try:
                serve.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                serve.proc.kill()
        if ticker is not None:
            ticker.stop()
        server.stop()
        stop_all()


if __name__ == "__main__":
    code = 1
    try:
        code = main()
        print("FRONTEND SMOKE PASSED")
    except AssertionError as exc:
        print(f"[FAIL] {exc}")
    except Exception as exc:  # noqa: BLE001 - report the crash, keep the receipts
        print(f"[FAIL] {type(exc).__name__}: {exc}")
    sys.exit(code)
