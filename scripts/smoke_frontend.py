#!/usr/bin/env python
"""HUD frontend smoke: built bundle, real ``serve`` process, real Chromium.

Follows ``scripts/smoke.py`` conventions: one printed receipt per observation,
non-zero exit on the first failed check. The browser is driven through
``scripts/browser_driver.py`` (stdlib CDP) against the fixture collector.

The checks cover the dark HUD dashboard (``docs/hud-dashboard-implementation-plan.md``)
and the contracts inherited from deliveries 1-4: testids, placeholders, token strings,
timer counts, the fetched API paths, diagnostics, the API cost well and the
1080x1920 portrait fit.
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
from decimal import Decimal
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
    RATE_ENV,
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
COST_SHOTS = ROOT / "docs" / "receipts" / "cost-estimate"
HUD_SHOTS = ROOT / "docs" / "receipts" / "hud-dashboard"
SECOND = {"model_name": "second-model", "engine": "0"}
FOCUS_EXPECTED = ["range-24h", "range-7d", "range-30d", "custom-from", "custom-to",
                  "custom-apply", "chart-disclosure", "requests-disclosure"]
DESKTOP = (1440, 900)
MOBILE = (390, 844)
PORTRAIT = (1080, 1920)

# Strings that belong to the design mockup, never to the dashboard it models.
MOCKUP_ONLY = ("SIMULATED", "Signal history", "design mockup")

# Installed before every document: counts poll timers, records fetch URLs, and
# can delay a response for URLs containing ``delayMatch`` (selection race).
PROBE = """
(() => {
  // The delay is armed through sessionStorage: the probe is re-installed on every
  // document, so a value written into the live object cannot survive the navigation
  // whose request it is meant to slow. Opaque-origin documents (about:blank) have no
  // sessionStorage at all, so the read is guarded.
  let dMs = 0, dMatch = "";
  try {
    dMs = Number(sessionStorage.getItem("__delayMs") || 0);
    dMatch = sessionStorage.getItem("__delayMatch") || "";
  } catch (e) { dMs = 0; dMatch = ""; }
  const p = {timers: [], cleared: [], fetches: [], delayMs: dMs, delayMatch: dMatch};
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


def arm_delay(page, ms: int, match: str = "/api/v1/summary") -> None:
    """Arm a response delay for the NEXT document, then disarm with ``arm_delay(page, 0)``.

    A delay written into the live probe is wiped when a navigation installs the fresh
    document's probe, so it is carried in sessionStorage, which the probe reads at init.
    """
    page.evaluate(
        "(() => { try { sessionStorage.setItem('__delayMs', "
        + json.dumps(str(ms))
        + "); sessionStorage.setItem('__delayMatch', "
        + json.dumps(match)
        + "); } catch (e) { /* opaque origin */ } })()"
    )


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


def header_box_heights(page) -> dict:
    """Item 10: the update box and the health badge are boxes of one height."""
    return page.evaluate(
        "(() => { const box = document.querySelector('[data-testid=\"last-update\"]')"
        ".closest('.hbox').getBoundingClientRect().height;"
        " const badge = document.querySelector('[data-testid=\"status-badge\"]')"
        ".getBoundingClientRect().height;"
        " return {box: Math.round(box * 10) / 10, badge: Math.round(badge * 10) / 10,"
        " equal: Math.abs(box - badge) < 1}; })()"
    )


def short_tokens(value) -> str:
    """Mirror the bundle's `formatTokensShort`: 6,941,333 -> `6.94 M`, 960 -> `960`."""
    if value is None:
        return "no data"
    tokens = int(value)
    for unit, suffix in ((10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "K")):
        if tokens < unit:
            continue
        hundredths = (tokens * 100 + unit // 2) // unit
        fraction = f"{hundredths % 100:02d}".rstrip("0")
        return f"{hundredths // 100:,}{('.' + fraction) if fraction else ''} {suffix}"
    return f"{tokens:,}"


def ring_radii(page, scope: str) -> list[int]:
    """The concentric ring radii in DOM (window) order, read from each track arc."""
    return page.evaluate(
        f"(() => [...document.querySelectorAll({json.dumps(scope + ' .rings-svg .track')})]"
        ".map((p) => Number(/A([\\d.]+)/.exec(p.getAttribute('d'))[1])))()"
    )


def ring_row_insets(page) -> dict:
    """Item 11: the ring art's left inset and the value column's right inset.

    The outer ring (r 46 plus its 6.5 glow) starts (66 - 52.5) of 132 viewBox units
    inside the svg box, so the art is inset from the well border even when the svg
    box is flush with the padding. A value column that sits on the padding edge is
    visually tighter than the art is.
    """
    return page.evaluate(
        "(() => { const rings = document.querySelector('[data-testid=\"live-panel\"]"
        " .live-left .rings'); const well = rings.closest('.well');"
        " const svg = rings.querySelector('svg');"
        " const w = well.getBoundingClientRect(), s = svg.getBoundingClientRect();"
        " const left = (s.left - w.left) + s.width * ((66 - 52.5) / 132);"
        " const right = w.right - Math.max(...[...rings.querySelectorAll('.rrow b')]"
        ".map((b) => b.getBoundingClientRect().right));"
        " return {left: Math.round(left * 10) / 10, right: Math.round(right * 10) / 10,"
        " balanced: Math.abs(left - right) <= 2}; })()"
    )


def shot(page, name: str) -> None:
    path = page.screenshot(SHOTS / name)
    receipt(f"screenshot {name}", f"{path} ({path.stat().st_size} bytes)")


def live_shot(page, name: str) -> None:
    path = page.screenshot(LIVE_SHOTS / name)
    receipt(f"screenshot {name}", f"{path} ({path.stat().st_size} bytes)")


def cost_shot(page, name: str) -> None:
    path = page.screenshot(COST_SHOTS / name)
    receipt(f"screenshot {name}", f"{path} ({path.stat().st_size} bytes)")


def hud_shot(page, name: str) -> None:
    path = page.screenshot(HUD_SHOTS / name)
    receipt(f"screenshot {name}", f"{path} ({path.stat().st_size} bytes)")


def live_slots(page, kind: str) -> str:
    """Join one live ratio group's per-window values in DOM (window) order.

    The HUD gives every window its own testid (``live-mtp-5s`` …), so the joined
    reading is what the old single ``live-mtp`` element used to carry: the joined
    slots string, e.g. ``"70% / - / - / 70%"``.
    """
    return page.evaluate(
        f"(() => [...document.querySelectorAll('[data-testid^=\"live-{kind}-\"]')]"
        ".map((el) => el.textContent.trim()).join(' / '))()"
    )


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
    # AC4: no cost line and no second axis on the trend chart. Datasets and axes are
    # painted on the canvas, so the chart module's source is the checkable surface.
    chart = (ROOT / "frontend" / "src" / "lib" / "chart.ts").read_text()
    check("chart_source_is_bar_only", 'type: "line"' in chart, False)
    check("chart_source_single_y_axis", re.search(r"\by1\s*:", chart) is not None, False)
    check("chart_source_two_datasets", chart.count('label: "'), 2)
    # AC4: the mockup's review machinery (the `s` hotkey, the #debug/#test overlays,
    # seeded fixtures, the STATE menu) must not have been ported into the app source.
    forbidden = ("keydown", "data-audit", "data-state", "__reviewHooks", "#debug",
                 "#test", "SIMULATED", "mulberry", "seedHist", "design mockup")
    offenders: list[str] = []
    for path in sorted((ROOT / "frontend" / "src").rglob("*")):
        if path.suffix not in {".ts", ".vue", ".css"}:
            continue
        text = path.read_text()
        offenders += [f"{path.name}:{token}" for token in forbidden if token in text]
    receipt("mockup_machinery_offenders", offenders)
    check("no_mockup_machinery_in_source", offenders, [])


def start_stack(server: FixtureServer, db: Path, source: str = "smoke",
                rates: dict[str, str] | None = None) -> ServeProc:
    port = free_port()
    serve = ServeProc(db, server.base_url, port)
    serve.env["SOURCE_ID"] = source
    serve.env["STATIC_DIR"] = str(STATIC)
    if rates:
        serve.env.update(rates)
    serve.start()
    ok = wait_for(lambda: get_path(port, "/health/ready")[0] == 200, 30)
    check(f"serve ready on port {port}", ok, True)
    return serve


def publish(server: FixtureServer, ci, go, ri=None, ro=None, ca=None) -> None:
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
                        None if ri is None else str(ri), None if ro is None else str(ro),
                        None if ca is None else str(ca))

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


def dom_basics(page, port: int) -> None:
    print("--- phase 4: built bundle renders the API values")
    started = time.monotonic()
    ok = wait_for(lambda: text_of(page, "lifetime-total") == "960", 40)
    receipt("dom_first_paint_seconds", round(time.monotonic() - started, 1))
    check("dom_renders_lifetime_total", ok, True)
    check("product name", text_of(page, "product-name"), "Nano LLM Usage - JARVIS")
    # AC5: the product line and the loaded model name share one line in the title.
    title = page.evaluate(
        "(() => { const h1 = document.querySelector('.brand .t');"
        " const mark = h1.querySelector('.mark'), model = h1.querySelector('.series-name');"
        " const a = mark.getBoundingClientRect(), b = model.getBoundingClientRect();"
        " return {text: h1.innerText.replace(/\\s+/g, ' ').trim(),"
        " oneLine: b.top < a.bottom - 1 && a.top < b.bottom - 1 && b.left > a.right}; })()"
    )
    receipt("header_title", title["text"])
    check("title_and_model_on_one_line_ac5", title["oneLine"], True)
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
    # AC4/AC5: nothing from the mockup's simulation layer, and one dark palette only.
    body = page.evaluate("document.body.innerText") or ""
    for token in MOCKUP_ONLY:
        check(f"no_mockup_text[{token}]", token.lower() in body.lower(), False)
    check("no_state_menu", "STATE" in body, False)
    check("no_theme_toggle", count_of(page, '[data-testid="theme-toggle"]'), 0)
    check("no_theme_attribute", page.evaluate(
        "document.documentElement.hasAttribute('data-theme')"), False)
    check("palette_is_dark", page.evaluate(
        "getComputedStyle(document.documentElement).colorScheme"), "dark")
    check("panel_order", page.evaluate(
        "(() => [...document.querySelectorAll('.hud > .pnl')]"
        ".map((el) => el.getAttribute('data-testid')))()"),
        ["app-header", "live-panel", "trend-panel", "summary-panel",
         "rates-panel", "diagnostics-panel"])
    # AC2 (change list): a healthy collection draws no status bar at all. The strip is
    # a problem surface (API failure, offline, stale, starting, loading, empty), so a
    # nominal line is the one state it must not render.
    check("nominal_bar_removed", count_of(page, '[data-testid="status-strip"]'), 0)
    check("nominal_copy_removed", "Collection healthy \u00b7 live 1 Hz" in body, False)
    # AC6 survives the bar: one polite region, in the header, naming the last update.
    check("polite_region_count", count_of(page, '[aria-live="polite"]'), 1)
    check("polite_line_is_status_region", page.evaluate(
        "(() => { const el = document.querySelector('[data-testid=\"status-live\"]');"
        " return [el.getAttribute('role'), el.getAttribute('aria-live')].join(' '); })()"),
        "status polite")
    polite = text_of(page, "status-live") or ""
    receipt("polite_line", polite)
    check("polite_line_names_the_update_ac6", bool(re.fullmatch(
        r"Healthy\. Last successful update \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\.", polite)), True)
    check("polite_line_is_not_visible_ink", page.evaluate(
        "(() => { const r = document.querySelector('[data-testid=\"status-live\"]')"
        ".getBoundingClientRect(); return r.width <= 1 && r.height <= 1; })()"), True)
    # AC10: the update box and the health badge are boxes of one height.
    receipt("header_box_heights", header_box_heights(page))
    check("header_boxes_same_height_ac10", header_box_heights(page)["equal"], True)
    shot(page, "desktop-1440-normal.png")


def layout(page) -> None:
    """AC1: the seven panels fill 1080x1920 without scrolling, scroll vertically elsewhere."""
    print("--- phase 4b: 1080x1920 fit, vertical-only scrolling on the smaller viewports")
    page.set_viewport(*PORTRAIT)
    metrics = page.evaluate(
        "({h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth,"
        " vh: window.innerHeight, vw: window.innerWidth})"
    )
    receipt("portrait_metrics", json.dumps(metrics))
    # The panels clip by design (plan D2), so "it fits" is not the whole story: record
    # what is being clipped. Non-empty lists mean content the 1080x1920 frame hides.
    receipt("portrait_clipped_panel_bodies", page.evaluate(
        "JSON.stringify([...document.querySelectorAll('.hud > .pnl > .in')]"
        ".filter((e) => e.scrollHeight > e.clientHeight + 1)"
        ".map((e) => ({head: (e.querySelector('h2') || {textContent: '?'}).textContent.trim(),"
        " client: e.clientHeight, content: e.scrollHeight})))"))
    receipt("portrait_clipped_wells", page.evaluate(
        "JSON.stringify([...document.querySelectorAll('.well')]"
        ".filter((e) => e.scrollHeight > e.clientHeight + 1)"
        ".map((e) => ({label: (e.querySelector('.lab') || {textContent: '?'}).textContent.trim(),"
        " client: e.clientHeight, content: e.scrollHeight})))"))
    check("portrait_no_vertical_scroll", metrics["h"] <= 1920, True)
    check("portrait_no_horizontal_scroll", metrics["w"] <= 1080, True)
    check("portrait_panels_without_nominal_bar", count_of(page, ".hud > .pnl"), 6)
    receipt("portrait_header_box_heights", header_box_heights(page))
    check("portrait_header_boxes_same_height_ac10", header_box_heights(page)["equal"], True)
    hud_shot(page, "hud-1080x1920-normal.png")

    for width, height in (DESKTOP, (1000, 900), MOBILE):
        page.set_viewport(width, height)
        small = page.evaluate(
            "({h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth,"
            " vh: window.innerHeight, vw: window.innerWidth})"
        )
        receipt(f"metrics_{width}x{height}", json.dumps(small))
        check(f"no_horizontal_scroll[{width}]", small["w"] <= small["vw"] + 1, True)
        check(f"vertical_scroll_allowed[{width}]", small["h"] >= small["vh"] - 1, True)
    page.set_viewport(*MOBILE)
    check("live_panel_inside_390", page.evaluate(
        "(() => document.querySelector('[data-testid=\"live-panel\"]')"
        ".getBoundingClientRect().right <= window.innerWidth + 1)()"), True)
    hud_shot(page, "hud-390-normal.png")
    page.set_viewport(1000, 900)
    hud_shot(page, "hud-1000-normal.png")
    page.set_viewport(*DESKTOP)
    hud_shot(page, "hud-1440-normal.png")


def ranges(page) -> None:
    print("--- phase 5: range presets and the inclusive custom end")
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

    # AC7: the custom range is always visible and sits on the right of the preset row.
    check("custom_range_always_visible", visible(page, "range-custom"), True)
    check("custom_fields_visible_without_opening",
          all(visible(page, field) for field in ("custom-from", "custom-to", "custom-apply")), True)
    receipt("custom_range_placement", json.dumps(page.evaluate(
        "(() => { const r = (el) => { const b = el.getBoundingClientRect();"
        " return {left: Math.round(b.left), right: Math.round(b.right),"
        " width: Math.round(b.width)}; };"
        " return {ctl: r(document.querySelector('.trend-ctl')),"
        " grp: r(document.querySelector('.trend-ctl .grp')),"
        " box: r(document.querySelector('[data-testid=\"range-custom\"]')),"
        " utc: r(document.querySelector('.trend-ctl .utc'))}; })()")))
    placement = page.evaluate(
        "(() => { const r = (el) => { const b = el.getBoundingClientRect();"
        " return {left: b.left, right: b.right, width: b.width}; };"
        " return {ctl: r(document.querySelector('.trend-ctl')),"
        " grp: r(document.querySelector('.trend-ctl .grp')),"
        " box: r(document.querySelector('[data-testid=\"range-custom\"]')),"
        " utc: r(document.querySelector('.trend-ctl .utc'))}; })()")
    check("custom_range_right_of_presets_ac7", placement["box"]["left"] >= placement["grp"]["right"], True)
    # The custom fields take the right-hand end of the row: the UTC tag is the only thing
    # past them, and they clear the row's midpoint. A left-packed row fails this.
    gap = float((page.evaluate(
        "getComputedStyle(document.querySelector('.trend-ctl')).columnGap") or "0").rstrip("px"))
    receipt("trend_control_gap", gap)
    check("custom_range_ends_the_row_ac7",
          placement["utc"]["right"] >= placement["ctl"]["right"] - 1
          and placement["box"]["right"] >= placement["utc"]["left"] - gap - 1
          and placement["box"]["right"] <= placement["utc"]["left"] + gap + 1
          and placement["ctl"]["right"] - placement["box"]["right"] <= placement["ctl"]["width"] * 0.25
          and placement["box"]["right"] - placement["ctl"]["left"] >= placement["ctl"]["width"] * 0.5,
          True)
    check("custom_range_shares_the_preset_row_ac7", page.evaluate(
        "(() => { const grp = document.querySelector('.trend-ctl .grp').getBoundingClientRect();"
        " const box = document.querySelector('[data-testid=\"range-custom\"]').getBoundingClientRect();"
        " return box.top < grp.bottom && grp.top < box.bottom; })()"), True)
    # The HUD styles micro-labels in caps, so the check reads the rendered text as-is.
    check("custom_end_label", "utc end (inclusive)" in (page.evaluate("document.body.innerText") or "").lower(), True)
    check("custom_end_label_is_the_end_input", page.evaluate(
        "(() => { const to = document.querySelector('[data-testid=\"custom-to\"]');"
        " const label = to && document.querySelector(`label[for=\"${to.id}\"]`);"
        " return label ? label.textContent.trim().toLowerCase() : null; })()"),
        "utc end (inclusive)")
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
    # AC4: the chart is input/output bars only. Chart.js paints datasets and axes onto
    # the canvas, so the HTML legend is the observable surface for "no cost series".
    check("legend_series_swatches_ac4", count_of(page, '[data-testid="trend-panel"] .legend i'), 2)
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
    page.set_viewport(*PORTRAIT)
    offline_metrics = page.evaluate(
        "({h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth})"
    )
    receipt("offline_portrait_metrics", offline_metrics)
    check("offline_portrait_no_vertical_scroll", page.evaluate(
        "document.documentElement.scrollHeight") <= 1920, True)
    check("offline_portrait_no_horizontal_scroll", page.evaluate(
        "document.documentElement.scrollWidth") <= 1080, True)
    hud_shot(page, "hud-1080x1920-offline.png")
    page.set_viewport(*DESKTOP)
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
    check("no_signal_history", count_of(page, '[data-testid="signal-history"]'), 0)
    # No draft counters, no experts block and no running requests yet: five placeholders.
    check("live_mtp_placeholder_before_counters", live_slots(page, "mtp"), "- / - / - / -")
    check("live_moe_placeholder_before_experts", live_slots(page, "moe"), "- / - / -")

    server.state.start_live_advancer(labels=L)
    expect(page, lambda: text_of(page, "live-requests") == "3 / 1", "live AC1 requests")
    check("live_state_ac1", text_of(page, "live-state"), "Live")
    check("live_prefill_ac1", text_of(page, "live-prefill"), "1,240")
    check("live_decode_ac1", text_of(page, "live-decode"), "180")
    # AC4 (change list): the unit lives in the label above the number, so the number is
    # bare; the per-request reading is the gauge's only supporting caption.
    receipt("live_gauge_labels", page.evaluate(
        "(() => [...document.querySelectorAll('[data-testid=\"live-panel\"] .gauge .g-body .lab')]"
        ".map((el) => el.textContent.trim()))()"))
    check("live_gauge_labels_name_the_unit_ac4", page.evaluate(
        "(() => [...document.querySelectorAll('[data-testid=\"live-panel\"] .gauge .g-body .lab')]"
        ".map((el) => el.textContent.trim()))()"),
        ["Prefill \u00b7 TPS", "Decode \u00b7 TPS"])
    check("live_instantaneous_captions_removed_ac4", page.evaluate(
        "document.querySelectorAll('[data-testid=\"live-panel\"] .gauge .g-unit').length"), 1)
    check("live_decode_per_request_ac4", text_of(page, "live-decode-per"),
          "60/req \u00b7 per running request")
    # AC4: the live line chart follows the MTP sparkline's style — a short, fluid box,
    # not the dial's 114x100 square, which is what warped the line.
    receipt("live_gauge_spark_box", page.evaluate(
        "(() => { const svg = document.querySelector('[data-testid=\"live-panel\"]"
        " .gauge .spark svg'); const r = svg.getBoundingClientRect();"
        " return {w: Math.round(r.width), h: Math.round(r.height),"
        " box: svg.getAttribute('viewBox')}; })()"))
    check("live_gauge_spark_matches_mtp_ac4", page.evaluate(
        "(() => { const gauge = document.querySelector('[data-testid=\"live-panel\"]"
        " .gauge .spark svg');"
        " const mtp = document.querySelector('[data-testid=\"live-panel\"] .live-left .spark svg');"
        " const g = gauge.getBoundingClientRect(), m = mtp.getBoundingClientRect();"
        " return mtp.getAttribute('viewBox') === gauge.getAttribute('viewBox')"
        " && Math.abs(g.height - 30) <= 1 && Math.abs(g.height - m.height) <= 1"
        " && g.width > 120 && g.width > g.height * 3; })()"), True)
    # AC6 (change list): the short rolling window is the outer ring, not the inner one.
    receipt("mtp_ring_radii", ring_radii(page, '[data-testid="live-panel"] .live-left'))
    check("mtp_rings_shortest_window_outermost_ac6", ring_radii(page, '[data-testid="live-panel"] .live-left'),
          [46, 33, 20])
    check("moe_rings_shortest_window_outermost_ac6", ring_radii(page, '[data-testid="live-panel"] .live-right'),
          [46, 33, 20])
    # AC11: the value column is as far from the panel border as the ring art is.
    receipt("mtp_row_insets", ring_row_insets(page))
    check("mtp_rows_equally_inset_ac11", ring_row_insets(page)["balanced"], True)
    # AC9 (change list): the live panel carries no glossary widget; the slots are labeled.
    check("live_glossary_widget_removed_ac9", count_of(page, '.live-hint'), 0)
    check("live_glossary_copy_removed_ac9",
          "MoE GPU hit is expert residency" in (text_of(page, "live-panel") or ""), False)
    # Lifetime needs a second /metrics sample, so it lands about a second after the
    # instantaneous stats tiles - a dash here is the engine refusing a one-point ratio.
    expect(page, lambda: (live_slots(page, "mtp") or "").endswith("70%"), "live AC1 MTP lifetime", 20)
    mtp_slots = (live_slots(page, "mtp") or "").split(" / ")
    check("live_mtp_windows_order", [s.strip() for s in mtp_slots][3], "70%")
    check("live_mtp_windows_unfilled_ac1", mtp_slots[2], "-")
    # AC7: the visuals draw only received values. Both gauges have a value arc, the
    # gauge scale is the nice ceiling of the received peak printed compact
    # (1,240 -> 2,000 -> "2K"; 180 -> 200), and the MTP rings draw one arc for the
    # one window that has a value. An arc per empty window would fail this.
    check("gauge_value_arcs_ac7", page.evaluate(
        "document.querySelectorAll('[data-testid=\"live-panel\"] .gauge .dial .value').length"), 2)
    receipt("gauge_end_labels", page.evaluate(
        "(() => [...document.querySelectorAll('[data-testid=\"live-panel\"] .gauge .dial .end-label')]"
        ".map((t) => t.textContent))()"))
    check("gauge_scale_is_nice_ceiling_ac7", page.evaluate(
        "(() => [...document.querySelectorAll('[data-testid=\"live-panel\"] .gauge .dial .end-label')]"
        ".map((t) => t.textContent))()"), ["0", "2K", "0", "200"])
    # AC7: the rings draw one arc per received window and none for a window the API
    # left empty, so the arc count must equal the rows that show a reading.
    receipt("live_mtp_rings", page.evaluate(
        "(() => { const well = [...document.querySelectorAll('[data-testid=\"live-panel\"] .well')]"
        ".find((w) => w.textContent.includes('MTP acceptance'));"
        " const rows = [...well.querySelectorAll('.rrow')].slice(0, 3);"
        " return [well.querySelectorAll('.rings-svg .value').length,"
        " rows.filter((r) => r.querySelector('b').textContent.trim() !== '-').length]; })()"))
    check("rings_arc_per_received_window_ac7", page.evaluate(
        "(() => { const well = [...document.querySelectorAll('[data-testid=\"live-panel\"] .well')]"
        ".find((w) => w.textContent.includes('MTP acceptance'));"
        " const rows = [...well.querySelectorAll('.rrow')].slice(0, 3);"
        " return well.querySelectorAll('.rings-svg .value').length"
        " === rows.filter((r) => r.querySelector('b').textContent.trim() !== '-').length; })()"), True)
    live_shot(page, "live-1440-ac1.png")

    # Zero running requests: the per-request slot is undefined, so it is a dash -
    # and the instantaneous throughput beside it stays real (plan AC1).
    server.state.set_live(running=0, waiting=1, prefill_tps=1240.4, decode_tps=180.2)
    expect(page, lambda: (text_of(page, "live-decode-per") or "").startswith("-"),
           "live per-request placeholder")
    check("live_requests_zero_running", text_of(page, "live-requests"), "0 / 1")
    check("live_decode_value_kept", text_of(page, "live-decode"), "180")
    server.state.set_live(running=3, waiting=1, prefill_tps=1240.4, decode_tps=180.2)

    expect(page, lambda: (live_slots(page, "mtp") or "").startswith("70% / - / - /"),
           "live MTP 5 s window", 30)
    # MoE lags MTP by a poll here: the two `running` edits above clear the experts block
    # for a second each, so its baseline point is slightly younger.
    expect(page, lambda: (live_slots(page, "moe") or "").startswith("93% / - / -"),
           "live MoE 5 s window", 30)
    receipt("live_5s_window_seconds", round(time.monotonic() - started, 1))
    # The MoE rows show the readings the API sent; the rings draw exactly that many arcs.
    receipt("live_moe_rings", page.evaluate(
        "(() => { const well = [...document.querySelectorAll('[data-testid=\"live-panel\"] .well')]"
        ".find((w) => w.textContent.includes('MoE'));"
        " const rows = [...well.querySelectorAll('.rrow')].slice(0, 3);"
        " return [well.querySelectorAll('.rings-svg .value').length,"
        " rows.filter((r) => r.querySelector('b').textContent.trim() !== '-').length]; })()"))
    check("rings_arc_per_received_window_moe_ac7", page.evaluate(
        "(() => { const well = [...document.querySelectorAll('[data-testid=\"live-panel\"] .well')]"
        ".find((w) => w.textContent.includes('MoE'));"
        " const rows = [...well.querySelectorAll('.rrow')].slice(0, 3);"
        " return well.querySelectorAll('.rings-svg .value').length"
        " === rows.filter((r) => r.querySelector('b').textContent.trim() !== '-').length; })()"), True)
    live_shot(page, "live-1440-window-5s.png")

    expect(page, lambda: (live_slots(page, "mtp") or "").startswith("70% / 70% / - /"),
           "live MTP 1 m window", 100)
    expect(page, lambda: (live_slots(page, "moe") or "").startswith("93% / 93% / -"),
           "live MoE 1 m window", 100)
    receipt("live_1m_window_seconds", round(time.monotonic() - started, 1))
    live_shot(page, "live-1440-window-1m.png")

    # A Radiance restart is an uptime decrease. The stats run resets, so the MoE
    # windows - whose baseline lives in /stats - go blank and refill, while MTP,
    # measured from /metrics, keeps its windows (plan AC3).
    ticker.stop()
    server.state.uptime_s = 5.0
    expect(page, lambda: live_slots(page, "moe") == "- / - / -", "live MoE windows reset", 20)
    check("live_mtp_keeps_windows_across_stats_reset",
          (live_slots(page, "mtp") or "").startswith("70% / 70%"), True)
    check("live_instant_tile_survives_stats_reset", text_of(page, "live-prefill"), "1,240")
    live_shot(page, "live-1440-run-reset.png")
    ticker = UptimeTicker(server, base=200.0)  # resumes the chainable clock above the drop
    expect(page, lambda: (live_slots(page, "moe") or "").startswith("93%"),
           "live MoE windows refilled", 30)

    # Both endpoints failing: every tile a dash, the chip stale, no 15 s panel damage.
    server.state.stats_fail = True
    server.state.metrics_status = 500
    expect(page, lambda: text_of(page, "live-state") == "Stale", "live outage stale chip", 20)
    check("live_outage_requests", text_of(page, "live-requests"), "- / -")
    check("live_outage_prefill", text_of(page, "live-prefill"), "-")
    check("live_outage_decode", text_of(page, "live-decode"), "-")
    check("live_outage_decode_per_request", text_of(page, "live-decode-per"),
          "- \u00b7 per running request")
    check("live_outage_mtp", live_slots(page, "mtp"), "- / - / - / -")
    check("live_outage_moe", live_slots(page, "moe"), "- / - / -")
    # AC7: with nothing received, the dials draw tracks only - no needle at zero,
    # no arc, no value. A component that reads a missing ratio as 0.0 fails here.
    check("gauges_draw_nothing_when_no_value_ac7", page.evaluate(
        "document.querySelectorAll('[data-testid=\"live-panel\"] .gauge .dial .value').length"), 0)
    check("rings_draw_nothing_when_no_value_ac7", page.evaluate(
        "document.querySelectorAll('[data-testid=\"live-panel\"] .rings-svg .value').length"), 0)
    receipt("live_outage_seconds", round(time.monotonic() - started, 1))
    live_shot(page, "live-1440-stale.png")

    server.state.stats_fail = False
    server.state.metrics_status = None
    expect(page, lambda: text_of(page, "live-state") == "Live", "live outage recovers", 20)
    check("live_requests_after_outage", text_of(page, "live-requests"), "3 / 1")

    # Sparkline truthfulness (plan AC7): the outage and the hidden tab each left a null
    # in the ring buffer, so the polyline must be broken into more than one run.
    receipt("live_spark_runs", page.evaluate(
        "document.querySelectorAll('[data-testid=\"live-panel\"] .spark-svg polyline').length"))
    check("live_spark_breaks_on_gaps", page.evaluate(
        "document.querySelectorAll('[data-testid=\"live-panel\"] .spark-svg polyline').length") > 1,
        True)

    # The portrait sheet, with the panel fed by the live fixture.
    page.set_viewport(*PORTRAIT)
    expect(page, lambda: page.evaluate(
        "document.documentElement.scrollHeight <= 1920 && document.documentElement.scrollWidth <= 1080"),
        "1080x1920 fit with live data", 10)
    hud_shot(page, "hud-1080x1920-live.png")
    page.set_viewport(*DESKTOP)

    # No experts block at all (a non-MoE model): MoE reads as absent, MTP is untouched.
    server.state.suppress_experts = True
    expect(page, lambda: live_slots(page, "moe") == "- / - / -", "live MoE absent", 20)
    check("live_mtp_unaffected_by_absent_moe", (live_slots(page, "mtp") or "").endswith("70%"), True)
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


def spark_runs(page) -> int:
    """Polylines drawn by the live sparklines: one per uninterrupted sample run."""
    return count_of(page, '[data-testid="live-panel"] .spark-svg polyline')


def poll_lifecycle(page) -> None:
    print("--- phase 8: one 15 s poll timer and one 1 s live timer, both paused when hidden")
    intervals = probe(page, "p.timers.filter((t) => !p.cleared.includes(t.id)).map((t) => t.ms)")
    check("poll_timers", sorted(intervals), [1000, 15000])
    base = probe(page, "p.fetches.length")
    runs_before = spark_runs(page)
    page.set_visibility(True)
    time.sleep(8)
    check("polls_paused_when_hidden", probe(page, "p.fetches.length"), base)
    check("poll_timers_cleared_when_hidden",
          probe(page, "p.timers.filter((t) => !p.cleared.includes(t.id)).length"), 0)
    page.set_visibility(False)
    page.tab(1)
    ok = wait_for(lambda: probe(page, "p.fetches.length") > base, 30)
    check("poll_resumes", ok, True)
    # AC7: resuming from a hidden tab must break the sparkline, not connect the
    # pre-hide samples to the resumed ones. The 40-sample cap trims the front, so the
    # run count is reported and the assertion is that an uninterrupted line is not the
    # only thing left on screen after a period with no observations.
    receipt("spark_runs_before_hidden", runs_before)
    receipt("spark_runs_after_resume", spark_runs(page))
    check("spark_break_after_hidden_tab", spark_runs(page) >= 2, True)


def a11y(page, port: int) -> None:
    print("--- phase 10: one dark palette, reduced motion, visible keyboard focus")
    page.navigate(f"http://127.0.0.1:{port}/")
    check("no_theme_toggle", count_of(page, '[data-testid="theme-toggle"]'), 0)
    check("no_stored_theme", page.evaluate("localStorage.getItem('jarvis-theme')"), None)
    check("no_theme_attribute", page.evaluate(
        "document.documentElement.hasAttribute('data-theme')"), False)
    check("root_color_scheme_dark", page.evaluate(
        "getComputedStyle(document.documentElement).colorScheme"), "dark")
    page.set_media("prefers-reduced-motion", "reduce")
    # The chips come with the first dashboard refresh; a fresh mount shows the skeleton.
    expect(page, lambda: count_of(page, '[data-testid="range-24h"]') == 1, "range chips", 60)
    check("reduced_motion", page.evaluate(
        "getComputedStyle(document.querySelector('[data-testid=\"range-24h\"]')).transitionDuration"
    ), "0s")
    check("reduced_motion_sweep_stopped", page.evaluate(
        "(() => { const el = document.querySelector('.live-sweep');"
        " return el ? getComputedStyle(el).animationName : 'none'; })()"), "none")
    # D9 names the sweep, the chip pulse and the offline badge blink; all must stop.
    check("reduced_motion_chip_dot_stopped", page.evaluate(
        "(() => { const el = document.querySelector('[data-testid=\"live-state\"] .dot');"
        " return el ? getComputedStyle(el).animationName : 'none'; })()"), "none")
    check("reduced_motion_badge_stopped", page.evaluate(
        "(() => { const el = document.querySelector('[data-testid=\"status-badge\"]');"
        " return el ? getComputedStyle(el).animationName : 'none'; })()"), "none")
    page.set_media("prefers-reduced-motion", "no-preference")

    page.evaluate("document.activeElement && document.activeElement.blur()")
    seen = []
    for _ in range(len(FOCUS_EXPECTED) + 12):
        page.tab(1)
        seen.append(page.evaluate(
            "(() => { const el = document.activeElement;"
            " const box = el && el.closest('[data-testid]');"
            " return {id: box ? box.getAttribute('data-testid') : null,"
            " outline: el ? getComputedStyle(el).outlineWidth : null}; })()"
        ))
    # A date input is several Chromium stops (the fields, then its calendar glyph), so
    # consecutive stops inside one control are one stop of the order contract.
    order = []
    for item in seen:
        if not order or order[-1] != item["id"]:
            order.append(item["id"])
    receipt("keyboard_focus_sequence", order)
    check("keyboard_focus_order", order[:len(FOCUS_EXPECTED)], FOCUS_EXPECTED)
    # Every panel control in the audit draws the design's 2px focus ring; Chromium's own
    # internals (the date glyph and the parts past this audit) keep the UA ring, which is
    # visible — an unfocused-looking 0px ring anywhere in the walk is the failure.
    rings: dict[str, set[str]] = {}
    for item in seen:
        rings.setdefault(str(item["id"]), set()).add(str(item["outline"]))
    # Chromium paints its own ring on the date field's internal segment parts, which is
    # 3px and visible; every control the HUD styles draws the design's 2px ring.
    ua_ring = {"custom-from", "custom-to"}
    receipt("keyboard_focus_rings", {key: sorted(value) for key, value in rings.items()})
    check("keyboard_focus_visible", {
        key: sorted(value) for key, value in rings.items()
        if key in FOCUS_EXPECTED
        and not (value == {"2px"} or (key in ua_ring and value <= {"2px", "3px"}))
    }, {})
    check("keyboard_focus_never_invisible",
          [key for key, value in rings.items() if value == {"0px"}], [])


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


def select_series(page, port: int, labels: dict) -> str:
    """Drive the header picker to one series and return its id."""
    wanted = next(i["series_id"] for i in series_items(port)
                  if i["model_name"] == labels["model_name"])
    expect(page, lambda: count_of(page, '[data-testid="series-picker"]') == 1, "series picker", 60)
    page.evaluate(
        f"(() => {{ const el = document.querySelector('[data-testid=\"series-picker\"]');"
        f" el.value = {json.dumps(wanted)};"
        " el.dispatchEvent(new Event('change', {bubbles: true})); })()"
    )
    return wanted


def cost_of(port: int, series_id: str, key: str = "lifetime") -> dict:
    """The `api_equivalent` object the API reports for one range of one series."""
    return (summary_of(port, series_id).get("ranges", {}) or {}).get(key, {}) \
        .get("api_equivalent", {})


def money_text(amount: object) -> str:
    """Mirror the bundle's `formatMoney` so the DOM can be compared to the API.

    Amounts are exact 6-decimal strings; 10_000 of those units are one cent.
    """
    if not isinstance(amount, str):
        return "no data"
    micros = int(Decimal(amount) * 1_000_000)
    if 0 < micros < 5_000:
        return "< $0.01"
    cents = (micros + 5_000) // 10_000
    return f"${cents // 100:,}.{cents % 100:02d}"


def share_text(ratio: object) -> str:
    if not isinstance(ratio, (int, float)):
        return "no data"
    return f"{round(ratio * 100)}% of input"


def amount_text(amount: object) -> str:
    """The card's money line: an approximate sign in front of the rounded amount."""
    money = money_text(amount)
    return money if money == "no data" else f"\u2248 {money}"


def cost_estimate(page, server: FixtureServer, serve: ServeProc) -> None:
    """Cost plan AC1-AC8 at the presentation layer.

    The exact arithmetic is pinned by the query tests, the API tests and smoke.py
    scenario 9, which publishes a known priced volume against a real `serve`. This
    phase owns the presentation contract: the card renders a money line in the
    documented shape, the rates footnote quotes the configured prices, the
    coverage note follows the documented format, a series with no cache tracking
    reads "no data" and names its own diagnostics scope, and removing the prices
    at startup hides the money line while the price-independent lines stay.

    The rendered money and share are compared to the API's own `api_equivalent` for
    the same range, read at the same moment, so a formatter that disagrees with the
    contract (the 6-decimal amount string is micro-dollars, not mills) fails here and
    not only in the query tests.
    """
    print("--- phase 12: the API-equivalent estimate, cache share and coverage")
    # The first publish changes nothing (the request counters return at their last
    # observed value, so every delta is zero) and only baselines the cached counter.
    publish(server, 1905, 162, 100, 100, 600_000)
    time.sleep(TWO_POLLS)
    # One priced interval: +1,000,000 request input, +100,000 request output,
    # +600,000 cached input.
    publish(server, 2000, 170, 1_000_100, 100_100, 1_200_000)
    # AC3: before the summary and history land, the well must read "no data" - no
    # zero, no money. The delay is armed in sessionStorage so it survives the navigation
    # that reinstalls the probe; a value written into the live probe would be wiped and
    # the collector would answer before the well ever painted its placeholder.
    arm_delay(page, 3500)
    page.navigate(f"http://127.0.0.1:{serve.port}/")
    expect(page, lambda: text_of(page, "cost-today") == "no data", "cost well before its data", 20)
    check("cost_well_no_data_before_summary_ac3", text_of(page, "cost-today"), "no data")
    check("cost_well_no_data_before_history_ac3", text_of(page, "cost-period"), "no data")
    check("cost_well_no_money_before_data_ac3", "$" in (text_of(page, "cost-panel") or ""), False)
    arm_delay(page, 0)
    # Earlier phases leave the page on the second series, so point the page at the
    # priced one before reading any money.
    priced = select_series(page, serve.port, L)
    # Deltas land on the collector's next poll, which is after the publish returns.
    time.sleep(TWO_POLLS)
    cost = cost_of(serve.port, priced)
    receipt("cost_api_lifetime", json.dumps(cost))
    receipt("cost_dom_rendered", {
        "cost": text_of(page, "lifetime-cost"),
        "cache": text_of(page, "lifetime-cache"),
        "footnote": (text_of(page, "cost-rates-footnote") or "")[:40],
    })
    # The card updates on the dashboard's own refresh cycle; reload so the fresh
    # totals are read immediately instead of waiting out a stall in the 15 s timer.
    page.navigate(f"http://127.0.0.1:{serve.port}/")
    select_series(page, serve.port, L)
    expect(page, lambda: visible(page, "cost-rates-footnote"), "rates footnote", 60)
    # The API's own answer for this range is the expectation; the card reads it on
    # its refresh cycle, so the rendered line must converge on it exactly.
    api_cost = cost_of(serve.port, priced)
    expected_money = amount_text(api_cost.get("amount"))
    expected_share = share_text(api_cost.get("cache_hit_ratio"))
    receipt("cost_api_expectation", f"{expected_money} | {expected_share}")
    expect(page, lambda: text_of(page, "lifetime-cost") == expected_money,
           f"priced money line {expected_money}", 90)
    money = text_of(page, "lifetime-cost") or ""
    receipt("cost_money_line", money)
    check("cost_money_line_ac1", money, expected_money)
    # Money renders in the documented shape: a rounded, grouped dollar figure,
    # the sub-cent floor, or the explicit no-data reading. A raw decimal string
    # reaching the DOM (the schema's 6-dp form) is the defect this rejects.
    check("cost_money_line_shape_ac1", bool(re.fullmatch(
        r"\u2248 \$[\d,]+\.\d{2}|< \$0\.01|no data", money)), True)
    share = text_of(page, "lifetime-cache") or ""
    receipt("cost_share_line", share)
    check("cost_share_line_ac1", share, expected_share)
    # The cache donut is decoration for the same received ratio, never its own number.
    donut = page.evaluate(
        "(() => { const card = document.querySelector('[data-testid=\"card-lifetime\"]');"
        " const t = card && card.querySelector('.donut .pct');"
        " return t ? t.textContent : null; })()"
    )
    receipt("cost_donut_label", donut)
    check("cost_donut_matches_received_share_ac1", donut, expected_share.split(" of input")[0])
    footnote = text_of(page, "cost-rates-footnote") or ""
    check("cost_footnote_ac4", footnote.startswith(
        "Approximate API-equivalent at $3.00 input / $0.30 cached / $15.00 output per M tokens (USD)."),
        True)
    receipt("cost_footnote", footnote)
    # The month range is the one that must be partial: it expects every hour since
    # the first of the month and prices only the hours the cache counter exists in.
    coverage = text_of(page, "month-cost-coverage") or ""
    receipt("cost_coverage_note", coverage)
    check("cost_coverage_note_shape_ac2", coverage == "" or bool(re.fullmatch(
        r"partial \u00b7 covers [\d.]+ of [\d.]+ (days|h)", coverage)), True)
    receipt("cost_amount_api", cost.get("amount"))
    check("cost_no_skew_flag", visible(page, "lifetime-cost-flag"), False)
    # AC8 (change list): the card's large number is the short form of the same exact
    # total, and the element's title still carries the grouped figure.
    life = summary_of(serve.port, priced).get("ranges", {}).get("lifetime", {})
    receipt("summary_big_number", f"{text_of(page, 'lifetime-total')} | {life.get('processed_total')}")
    check("summary_big_number_is_short_form_ac8", text_of(page, "lifetime-total"),
          short_tokens(life.get("processed_total")))
    check("summary_big_number_keeps_the_exact_value_ac8", page.evaluate(
        "document.querySelector('[data-testid=\"lifetime-total\"]').getAttribute('title')"),
        dom_number(life.get("processed_total")))
    # The trend panel prices the selected range, so the 24 h line carries the same
    # money and share as the cards.
    page.click('[data-testid="range-24h"]')
    trend_prefix = f"API-equivalent {expected_money} \u00b7 Cache hits {expected_share}"
    # The panel prices its own range request, which lands after the cards' refresh.
    expect(page, lambda: (text_of(page, "trend-cost") or "").startswith(trend_prefix),
           f"trend cost line {trend_prefix}", 90)
    trend = text_of(page, "trend-cost") or ""
    receipt("cost_trend_line", trend)
    check("cost_trend_line_ac1", trend.startswith(trend_prefix), True)
    cost_shot(page, "cost-1440-estimate.png")

    # AC2: the live panel's API cost well agrees with the panels that own the numbers.
    expect(page, lambda: text_of(page, "cost-today") == text_of(page, "today-cost"),
           f"cost well today matches the today card ({text_of(page, 'today-cost')})", 90)
    receipt("cost_well_today", text_of(page, "cost-today"))
    period = text_of(page, "cost-period") or ""
    check("cost_well_period_matches_trend_ac2", period in trend, True)
    receipt("cost_well_period", f"{period} | {text_of(page, 'cost-period-span')}")
    check("cost_well_span_is_hourly_ac2", bool(re.fullmatch(
        r"24 h \u00b7 \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC \u2192 \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC",
        text_of(page, "cost-period-span") or "")), True)
    # AC3 (change list): the well keeps its own numbers and the period they cover, and
    # drops the caption and the coverage caption. Quality flags are not captions.
    check("cost_well_caption_removed_ac3", count_of(page, '[data-testid="cost-panel"] .cost-tag'), 0)
    check("cost_well_no_equivalent_caption_ac3",
          "API-equivalent" in (text_of(page, "cost-panel") or ""), False)
    check("cost_well_coverage_lines_removed_ac3", page.evaluate(
        "(() => { const well = document.querySelector('[data-testid=\"cost-panel\"]');"
        " return [...well.querySelectorAll('.cost-line')]"
        ".filter((l) => !l.classList.contains('is-flag'))"
        ".every((l) => l.getAttribute('data-testid') === 'cost-period-span'); })()"), True)
    hud_shot(page, "hud-1440-cost-well.png")
    # AC2: a preset change moves the well's period to the new history response.
    page.click('[data-testid="range-7d"]')
    expect(page, lambda: (text_of(page, "cost-period-span") or "").startswith("7 days \u00b7 "),
           "cost well period follows the 7d history", 90)
    check("cost_well_span_is_daily_ac2", (text_of(page, "cost-period-span") or "").endswith(" UTC"),
          True)
    check("cost_well_period_still_tracks_trend_ac2",
          (text_of(page, "cost-period") or "") in (text_of(page, "trend-cost") or ""), True)
    page.click('[data-testid="range-24h"]')
    expect(page, lambda: (text_of(page, "cost-period-span") or "").startswith("24 h \u00b7 "),
           "cost well period follows the 24h history", 90)

    page.set_viewport(*MOBILE)
    expect(page, lambda: visible(page, "lifetime-cost"), "mobile money line", 30)
    receipt("cost_mobile_money_line", text_of(page, "lifetime-cost"))
    cost_shot(page, "cost-390-estimate.png")
    page.set_viewport(*DESKTOP)

    # AC3: a series switch clears the well. The summary probe delays the incoming
    # response, so the window between the picker change and the new data landing is
    # observable: the old series' money must not stay on screen.
    second_id = next(i["series_id"] for i in series_items(serve.port)
                     if i["model_name"] == SECOND["model_name"])
    probe(page, "p.delayMs = 2500; p.delayMatch = '/api/v1/summary';")
    page.evaluate(
        f"(() => {{ const el = document.querySelector('[data-testid=\"series-picker\"]');"
        f" el.value = {json.dumps(second_id)};"
        " el.dispatchEvent(new Event('change', {bubbles: true})); })()"
    )
    expect(page, lambda: text_of(page, "cost-today") == "no data",
           "cost well cleared by the series switch", 20)
    check("cost_well_switch_today_ac3", text_of(page, "cost-today"), "no data")
    check("cost_well_switch_period_ac3", text_of(page, "cost-period"), "no data")
    check("cost_well_switch_no_stale_money_ac3",
          "$" in (text_of(page, "cost-panel") or ""), False)
    probe(page, "p.delayMs = 0; p.delayMatch = '';")
    expect(page, lambda: (text_of(page, "series-name") or "").startswith(SECOND["model_name"]),
           "switch to the second series lands", 60)

    # AC3 on a fresh load: the delay is armed in sessionStorage and read by the probe as
    # a document-init script, so it applies to the next navigation. The live panel is
    # painted while its summary and history are still in flight.
    arm_delay(page, 1500)
    page.navigate(f"http://127.0.0.1:{serve.port}/")
    expect(page, lambda: text_of(page, "cost-today") == "no data", "cost well before its data", 20)
    check("cost_well_no_data_before_summary_ac3", text_of(page, "cost-today"), "no data")
    check("cost_well_no_data_before_history_ac3", text_of(page, "cost-period"), "no data")
    check("cost_well_no_money_before_data_ac3", "$" in (text_of(page, "cost-panel") or ""), False)
    arm_delay(page, 0)

    # AC8: a series whose engine publishes no cached counter prices nothing, and
    # the diagnostics gap list names the cached-input scope as its own line.
    install_fixture(server.state, SECOND, 500, 50, 1_000, 100)
    assert wait_for(lambda: len(series_ids(serve.port)) >= 2, 60), "second series never appeared"
    # This fixture publishes the same counters the series was created with, so it stays at
    # its opening balance and every observed figure reads "no data". That reading is also
    # what a not-yet-loaded summary shows, so first prove this series' payload has landed:
    # the header name comes from the summary of the selected series.
    page.navigate(f"http://127.0.0.1:{serve.port}/")
    select_series(page, serve.port, SECOND)
    expect(page, lambda: SECOND["model_name"] in (text_of(page, "series-name") or ""),
           "second series summary landed", 60)
    expect(page, lambda: text_of(page, "lifetime-cost") == "no data", "unpriced series cost", 60)
    check("cost_unpriced_is_no_data_ac8", text_of(page, "lifetime-cost"), "no data")
    # AC3/AC7: switching series resets the well's figures — both read "no data" for a
    # series with no accepted interval, and the period line follows the new history.
    check("cost_well_switch_today_ac3", text_of(page, "cost-today"), "no data")
    check("cost_period_no_data_ac7", text_of(page, "cost-period"), "no data")
    # AC3: a series whose engine publishes no cached counter prices nothing, so the
    # well's own figures agree with the card above it.
    check("cost_well_unpriced_today_ac3", text_of(page, "cost-today"), "no data")
    check("cost_unpriced_share_ac8", text_of(page, "lifetime-cache"), "no data")
    unpriced = text_of(page, "lifetime-cost-coverage") or ""
    # A series created minutes ago has no complete hour yet, so coverage is
    # legitimately empty; once it has one, nothing is priced without cache tracking.
    check("cost_unpriced_coverage_ac8", unpriced == "" or unpriced.startswith("partial · covers 0"), True)
    receipt("cost_unpriced_coverage", unpriced)

    def diagnostics_text() -> str:
        open_details(page, "diagnostics-disclosure")
        return page.evaluate(
            "(() => document.querySelector('[data-testid=\"diagnostics-disclosure\"]')"
            ".innerText)()"
        )

    # The gap list only renders once the summary has landed, so this must be a
    # polling expectation and not a one-shot read of a page that is still loading.
    expect(page, lambda: "Cached input" in diagnostics_text(),
           "diagnostics names the cached-input gap", 60)
    receipt("cost_unpriced_diagnostics", diagnostics_text()[:200].replace("\n", " | "))
    cost_shot(page, "cost-1440-no-cached-counter.png")

    # AC4/AC6: rates are startup-only. The same volume without prices hides the
    # money line and the footnote, and keeps the cache share and coverage notes.
    publish(server, 2000, 170, 1_000_100, 100_100, 1_200_000)
    rc, _, _ = serve.stop()
    check("cost serve exits cleanly", rc, 0)
    serve.env.pop("COST_RATE_INPUT_PER_MTOK")
    serve.env.pop("COST_RATE_CACHED_INPUT_PER_MTOK")
    serve.env.pop("COST_RATE_OUTPUT_PER_MTOK")
    serve.start()
    assert wait_for(lambda: get_path(serve.port, "/health/ready")[0] == 200, 40), "restart not ready"
    page.navigate(f"http://127.0.0.1:{serve.port}/")
    expect(page, lambda: text_of(page, "lifetime-total") != "no data", "republished total", 60)
    if count_of(page, '[data-testid="series-picker"]') == 1:
        select_series(page, serve.port, L)
    expect(page, lambda: visible(page, "lifetime-cache"), "share line renders", 60)
    check("rates_free_hides_money_ac4", visible(page, "lifetime-cost"), False)
    check("rates_free_hides_footnote_ac4", visible(page, "cost-rates-footnote"), False)
    check("rates_free_keeps_share_ac4", bool(re.fullmatch(
        r"\d+% of input|no data", text_of(page, "lifetime-cache") or "")), True)
    check("rates_free_keeps_coverage_ac4",
          (text_of(page, "month-cost-coverage") or "").startswith("partial · covers "), True)
    cost_shot(page, "cost-1440-rates-unset.png")
    # AC3: the cost well explains the missing rates instead of showing an amount.
    expect(page, lambda: visible(page, "cost-unpriced"), "cost well rates note", 60)
    well = text_of(page, "cost-panel") or ""
    check("rates_free_cost_well_explains_ac3", well.startswith("Cost rates not configured"), True)
    check("rates_free_cost_well_has_no_amount_ac3", "$" in well, False)
    check("rates_free_cost_figures_absent_ac3", count_of(page, '[data-testid="cost-today"]'), 0)
    check("rates_free_cost_period_absent_ac3", count_of(page, '[data-testid="cost-period"]'), 0)
    hud_shot(page, "hud-1440-cost-unpriced.png")


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
    COST_SHOTS.mkdir(parents=True, exist_ok=True)
    HUD_SHOTS.mkdir(parents=True, exist_ok=True)
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
        serve = start_stack(server, tmp / "front.sqlite3", rates=RATE_ENV)
        receipt("cost rate env", sorted(serve.env[k] for k in RATE_ENV))

        accounting(server, serve.port)

        browser = Browser.launch(tmp / "chrome", [])
        receipt("chromium_pid", browser.pid)
        page = browser.new_page(*DESKTOP)
        page.add_script_on_new_document(PROBE)
        page.navigate(f"http://127.0.0.1:{serve.port}/")
        root_status, root_type = raw_get(serve.port, "/")
        receipt("root_response", f"{root_status} {root_type.split(';')[0] or 'no content-type'}")

        dom_basics(page, serve.port)
        layout(page)
        ranges(page)
        chart_integrity(page, server)
        empty_and_baseline(tmp, browser)
        offline(page, server)
        api_failure(page, serve)
        optional_unavailable(page, server)
        ticker = live_phase(page, server, ticker)
        poll_lifecycle(page)
        a11y(page, serve.port)
        selection_race(page, server, serve.port)
        network(page, server, serve.port)
        cost_estimate(page, server, serve)

        page.set_viewport(*MOBILE)
        shot(page, "mobile-390-normal.png")
        page.set_viewport(*DESKTOP)
        receipt("screenshots_written", len(list(SHOTS.glob("*.png"))))
        receipt("cost_screenshots_written", len(list(COST_SHOTS.glob("*.png"))))
        receipt("hud_screenshots_written", sorted(p.name for p in HUD_SHOTS.glob("*.png")))
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
