# HUD dashboard redesign — implementation evidence

Receipts: [`docs/receipts/hud-dashboard/`](receipts/hud-dashboard/). Plan:
[hud-dashboard-implementation-plan.md](hud-dashboard-implementation-plan.md). Design
source: [`docs/mockups/hud-dashboard-qwen.html`](mockups/hud-dashboard-qwen.html).
Date: 2026-10-27 (UTC). Contract: specification
[D7](specification.md#d7-visual-design-accessibility-responsive-layout), scenarios
[30–31](acceptance-scenarios.md#f-visual-design-and-accessibility-specification-d7).

## 1. Summary

- **Outcome:** the served UI is the mockup's seven-panel HUD running on real API
  data — header, collection-status strip, live, trend, period summary, latest rate,
  diagnostics — in one dark palette. The live panel's right column opens with the new
  **API cost** well (today UTC + the selected period, with its span and coverage),
  which replaces the mockup's "Signal history" well. No backend, API, OpenAPI, schema
  or query change: the well reads `summary.ranges.today`, `history.range` and
  `status.cost_rates` from the state the dashboard already polls, and the page makes
  no new request.
- **Removed:** the theme toggle, the `jarvis-theme` store, every `data-theme`
  selector, and the two banner components (`OfflineBanner`, `ApiErrorBanner`), whose
  message slots are now the single ranked line in the status strip. `ControlRow.vue`
  and `LabelValue.vue` were deleted once no component used them.
- **Not claimed:** the container-image gates (no `docker` on this host, §5), the
  real-engine operator gate (§5), and any human visual sign-off (§5).
- **Defects the browser phase caught:** five, all in production code — the portrait
  viewport overflowed (2487 px of content in a 1920 px frame) until the fixed grid rows
  were paired with `min-height: 0` on the panels and chart shell; the cost well
  rendered its head labels above the "Cost rates not configured" note, where AC3
  requires the note to be the well's only content; the trend panel body clipped 73 px
  (and, with it, the page gained scroll overflow) because the canvas' pixel height kept
  inflating its flex parent, fixed with the usual `position: relative` shell plus an
  absolutely positioned canvas; the MoE well clipped 61 px in the portrait column,
  fixed by giving `.live-right` a `minmax(0, 1fr)` middle row and a smaller ring dial;
  and the MTP rings drew a fourth ring from the lifetime window, outside the three
  radii the design defines, fixed by feeding the rings the three rolling windows only
  (the lifetime stays a printed row). That last one was found by an AC7 check that
  compares the ring arc count with the rows that show a reading. The final portrait
  pass reports no clipped panel body and no clipped well.

## 2. Environment (executed gates)

| Field | Value |
|---|---|
| OS / kernel | `Linux 7.2.11-1-cachyos (x86_64)` |
| Python | 3.13.17 (`.venv`); the project requires ≥3.11 |
| Node / npm | v24.22.0 / 11.19.1 |
| Browser | Chromium 156.0.8078.47, driven over CDP by `scripts/browser_driver.py` |
| Docker / compose | absent — `docker: command not found`; image gates NOT EXECUTED (§5) |
| Git | HEAD `f422b8d` on `main`; 45 tracked files changed (+2970/−871, including the receipt PNGs every smoke run re-captures) plus 12 new paths: 8 components, `lib/hudGeometry.ts`, this document, the plan, `docs/receipts/hud-dashboard/` |
| Upstream | `tests/fixtures/fixture_server.py` (loopback fixture). The real Radiance engine was **not** contacted. |

`PYTHONPATH=src` is required for `python -m jarvis_dashboard…` on this host; `pytest`
and the `scripts/*` harnesses set it themselves.

## 3. Commands and exit codes

| Command | Result | Exit |
|---|---|---|
| `cd frontend && npm ci` | dependencies installed (`node_modules` present) | 0 |
| `cd frontend && npx vue-tsc --noEmit` | no type errors | 0 |
| `cd frontend && npm run build` | `static/index.html` 0.58 kB, `assets/index-D40bSOZf.css` 24.08 kB (gzip 5.54), `assets/index-6aAx0y0N.js` 265.13 kB (gzip 94.22) | 0 |
| `PYTHONPATH=src .venv/bin/python scripts/export_openapi.py --check` | snapshot current (no API surface change); re-run as the smoke's first phase | 0 |
| `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_ui_tokens.py` | 21 passed | 0 |
| `PYTHONPATH=src .venv/bin/python -m pytest -q` | 374 passed in 43 s — no new failures | 0 |
| `PYTHONPATH=src .venv/bin/python scripts/smoke_frontend.py` | `FRONTEND SMOKE PASSED` — 164 checks, 0 failures (transcript: [`receipts/hud-dashboard/frontend-smoke.txt`](receipts/hud-dashboard/frontend-smoke.txt)) | 0 |
| `PYTHONPATH=src .venv/bin/python scripts/smoke.py` | backend end-to-end suite unchanged and green | 0 |

## 4. Acceptance criteria (plan AC1–AC8)

Names in the evidence column are `scripts/smoke_frontend.py` checks, run against the
built bundle served by the real collector with a loopback fixture.

| AC | Requirement | Evidence | Result |
|---|---|---|---|
| AC1 | Seven panels fill 1080×1920 with no scrolling; 1440×900 and 390×844 scroll vertically only; the live panel's right edge is inside a 390 px viewport | `portrait_metrics = {"h": 1920, "w": 1080, "vh": 1920, "vw": 1080}`, `portrait_no_vertical_scroll`, `portrait_no_horizontal_scroll`, `portrait_seven_panels` (7), `portrait_clipped_panel_bodies = []`, `portrait_clipped_wells = []`, `no_horizontal_scroll[1440/1000/390]`, `vertical_scroll_allowed[1440/1000/390]`, `live_panel_inside_390`, `offline_portrait_no_vertical_scroll` + `offline_portrait_no_horizontal_scroll` in the offline state | pass |
| AC2 | The live right column starts with the cost well; `cost-today` equals the today card; `cost-period` equals the amount inside the trend cost line; both follow a preset change; no "Signal history" | `cost_well_today = ≈ $2.88` (asserted equal to `today-cost`), `cost_well_period_matches_trend_ac2`, `cost_well_period = ≈ $2.88 \| 24 h · 2026-10-26 16:00 UTC → 2026-10-27 16:00 UTC`, `cost_well_span_is_hourly_ac2`, `cost_well_span_is_daily_ac2`, `cost_well_period_still_tracks_trend_ac2`, `no_signal_history` (0), `cost_donut_matches_received_share_ac1` (`60%` = the cache share the API sent) | pass |
| AC3 | `cost_rates` null → the well says "Cost rates not configured" with no `$`; before summary/history every figure reads `no data`; a series switch resets both | unpriced: `rates_free_cost_well_explains_ac3`, `rates_free_cost_well_has_no_amount_ac3`, `rates_free_cost_figures_absent_ac3` (0), `rates_free_cost_period_absent_ac3` (0); in flight: `cost_well_no_data_before_summary_ac3`, `cost_well_no_data_before_history_ac3`, `cost_well_no_money_before_data_ac3` (no `$`); on the series switch: `cost_well_switch_today_ac3`, `cost_well_switch_period_ac3`, `cost_well_switch_no_stale_money_ac3`, and `cost_well_unpriced_today_ac3` on the baseline series | pass |
| AC4 | No mockup scaffolding on the served page, and no cost line/second axis on the trend chart | `no_mockup_text[SIMULATED / Signal history / design mockup]`, `no_state_menu`, `canvas_count` (1 canvas), `legend_series_swatches_ac4` (2 swatches), `chart_source_is_bar_only`, `chart_source_single_y_axis`, `chart_source_two_datasets` (2), `no_mockup_machinery_in_source = []` — every `frontend/src` `.ts`/`.vue`/`.css` file scanned for `keydown`, `data-audit`, `data-state`, `__reviewHooks`, `#debug`, `#test`, `SIMULATED`, `mulberry`, `seedHist`, `design mockup` | pass |
| AC5 | One dark palette, no theme control, tokens pass contrast | `palette_is_dark` (`color-scheme: dark`), `root_color_scheme_dark`, `no_theme_toggle` (0), `no_stored_theme` (`localStorage` `jarvis-theme` null), `no_theme_attribute`; `tests/test_ui_tokens.py` — one `:root`, no `[data-theme]` block, 20 colour tokens, text pairs ≥ 4.5:1 on every surface they are drawn on, series pair ≥ 3:1, no raw hex outside `tokens.css` | pass |
| AC6 | Every pre-existing contract holds | the whole inherited suite ran unmodified in the same pass: `openapi snapshot current`, `api_lifetime_input/output` + `dom_lifetime_total/input/output` (900 / 60 / 960), `dom_utc_labelled` (3 `card-*`), `dom_lifetime_start_rendered`, live tiles (`live_tiles` 5, `live_requests_*` `3 / 1`, `live_prefill_ac1` `1,240 tok/s`, `live_decode_ac1` `180 tok/s · 60/req`), ranges (`range_24h_bucket`, `custom_end_exclusive`), chart table (`null_bucket_label`, `null_bucket_never_zero`), `baseline_total_is_not_zero`, `requests_history_kept_when_absent`, `state_offline_history_kept`, `api_failure_history_kept`, `poll_timers` `[1000, 15000]` + `poll_timers_cleared_when_hidden`, `race_no_stale_overwrite`, `browser_requests_local_only`, `console_clean`, `keyboard_focus_order`, `keyboard_focus_visible` (2 px), `strip_nominal_line_ac6` (`Collection healthy · live 1 Hz · collector polls every 5s · raw retention 30 days`, built from the API's own `poll_interval_seconds` / `raw_retention_days`), `polite_line_is_status_region` (`status polite`) and `polite_line_names_the_update_ac6`, and the exact token strings checked by `tests/test_ui_tokens.py` | pass |
| AC7 | Visuals render only received values; a failed poll and a hidden tab break the sparkline; reduced motion stops the HUD animation | `live_spark_runs = 5` / `live_spark_breaks_on_gaps` after a fixture outage, `spark_runs_before_hidden = 5` → `spark_runs_after_resume = 5` with `spark_break_after_hidden_tab` (the cap trims the front, so the count is stable and the break is the observable), `gauge_value_arcs_ac7` (2 dials draw) + `gauge_scale_is_nice_ceiling_ac7` (`['0', '2K', '0', '200']` — the 1 240.4 tok/s peak ends at 2K, the 180.2 peak at 200), `live_mtp_rings = [0, 0]` / `live_moe_rings = [1, 1]` with `rings_arc_per_received_window_ac7` + `…_moe_ac7` (one arc per window that shows a reading), `gauges_draw_nothing_when_no_value_ac7` + `rings_draw_nothing_when_no_value_ac7` (0 arcs during the outage), `reduced_motion` (`transitionDuration: 0s`), `reduced_motion_sweep_stopped` / `reduced_motion_chip_dot_stopped` / `reduced_motion_badge_stopped` (`animation-name: none`), `state_offline_history_kept` (values frozen, not invented) | pass |
| AC8 | The custom end input is labelled "UTC end (inclusive)" and picking day D still sends `to = D+1T00:00:00Z` | `custom_end_label`, `custom_end_label_is_the_end_input` (`'utc end (inclusive)'`), `custom_end_exclusive` (`to == {day(1)}T00:00:00.000Z`), `custom_from_midnight`, `custom_bucket` | pass |

## 5. NOT EXECUTED — limits

- **Container image gates.** `docker` is absent, so no image build, no
  `docker compose up`, no container-level screenshot. Same limitation as the
  delivery-3, live-panel and cost-estimate receipts.
- **Real engine.** Only the loopback fixture was contacted. The HUD renders fixture
  values; nothing here proves behaviour against a live Radiance engine.
- **No human visual review.** The agent that ran the gates cannot open the screenshot
  images, so the receipts in `docs/receipts/hud-dashboard/` are for a human to judge.
  Layout claims in §4 are geometric (scroll boxes, edges, counts), not aesthetic.
- **`color-mix()` support.** Every derived tint goes through `color-mix()`, which the
  plan accepts as a baseline (Chromium 111+, Firefox 113+, Safari 16.2+). Verified in
  Chromium 156; unverified on the other two engines.
- **The in-flight window is simulated, the network is not.** AC3's "before the data
  arrives" reading is produced by the harness's page probe (a document-init script that
  delays `/api/v1/summary` by 1.5 s) and by a real series switch, which clears the
  summary and history in the store. Both exercise the real component path; neither
  measures real network latency, and no server-side behaviour is implied.
- **Canvas pixels are not asserted.** The trend chart's colours, hatch and axis ticks
  are checked at source level (`chart_source_*`) and by geometry (one canvas, two
  datasets, two legend swatches); the smoke does not read canvas pixels, and this agent
  cannot open the screenshots.

## 6. Decisions and rationale

- **Overlay stacking (D9).** The page-wide grid/scanline/vignette layers are painted
  behind `.hud` (`z-index: 0` vs `1`) and every overlay is `pointer-events: none`, so
  no overlay can sit over a focus outline. The panel-level grid texture lives in each
  `.pnl > .in` background, where it cannot cover its own controls.
- **Product name (D4).** `<h1 class="t">` carries a visually-hidden
  `data-testid="product-name"` span with the exact text `Jarvis Token Dashboard`; the
  visible `JARVIS·TOKEN//HUD` mark is `aria-hidden`, so the accessible name and the
  existing test text are unchanged while the brand matches the mockup.
- **Live tile text (D4).** `live-requests` keeps `3 / 1` as one element by rendering
  `active` plus ` / queued` in a styled `span.small`; `live-prefill` and `live-decode`
  split `tok/s` into a `.g-unit` span for the gauge type scale. `textContent`
  concatenation is byte-identical to `joinSlots` / `formatTps` /
  `` `${formatTps} · ${formatPerRequest}` ``, which is what the smoke asserts.
- **Per-window MTP/MoE (D4).** The joined `live-mtp` / `live-moe` elements became
  `live-mtp-{5s,1m,30m,life}` and `live-moe-{5s,1m,30m}`, joined by the smoke's
  `live_slots()` helper in DOM order, so the ring rows are individually addressable
  without changing the rendered strings.
- **Cost well (D3).** `cost-today` uses the summary's own baseline rule, so the well
  and the today card say the same thing; `cost-period` reuses `history.range` and
  `formatHistorySpan` for the period label (`24 h · … UTC → … UTC` hourly,
  `7 days · D → D UTC` daily with the end day = `to − 1 day`). The well is not a
  `.live-cell`, which is why `live_tiles` stays 5. No `aria-live` region: the figures
  refresh every poll and a polite region would spam the announcement channel.
- **Status strip (D5).** One ranked message (API error → offline → series stale →
  starting → loading → empty → nominal health line) replaces both banners; the strip
  renders before the rates panel, so the first `[data-testid="stale-notice"]` in the
  document is the API-error notice, which is what the smoke reads.
- **Trend chart (D7).** Bar-only, two datasets, one y axis; legend moved to HTML
  (`24 h · hourly` style caption + span). An incomplete bucket is drawn with a canvas
  hatch pattern, a partial bucket with a 1 px `--warning` border. Table mode hides the
  canvas and legend with CSS (the canvas stays in the DOM) and calls `chart.resize()`
  on close, because chart.js cannot re-measure a zero-size container.
- **Rings, gauges, meters (D6).** Gauge full scale is the "nice" ceiling (1 / 2 / 2.5 / 5 ×
  10ⁿ) of the running peak since mount; the rate meters scale to the per-series session
  peak and reset on `selectedSeriesId` change. The rings draw the three rolling windows
  only — the MTP lifetime window is printed as a row, not ringed, matching the design's
  three radii. All three are display-only: the printed numbers come from the API, and
  with no peak the track is empty and the value reads `-`. Peaks reset on reload by
  design.
- **Custom range (D8).** Only the label changed. `paramsForRange` still sends the
  exclusive `to = end + 1 day`, and `range-custom` is still a `<details>` whose
  summary is styled as the fourth preset button, which keeps the focus order and the
  harness's `open_details()` working.
- **Cost well detail lines.** `formatCoverage` is a caption (`.cost-line`); the cache-skew
  note from `formatCostFlags` is a warning and takes the flag styling
  (`.cost-line.is-flag`), so the two cannot be confused in the narrow column.
- **Panels with no rates.** The period-summary cost row keeps its label and reads
  "cost rates not configured" when no rates are configured (plan step 7), so the card
  does not silently lose a row; the money testids stay hidden, which is what
  `rates_free_hides_money_ac4` asserts.
- **Diagnostics meta.** The panel head counts events, epochs, retention days and flags
  (`N events · N epochs · retention N d · N flags`) — the retention figure is what the
  plan's step 9 asks for, and `no data` covers a status that has not loaded.
- **Live note.** The starting-state copy is the plan's step 4 wording ("No engine sample
  accepted yet; every slot shows -."), not the mockup's, which described the mockup's own
  fixtures.

## 7. Files

- `frontend/src/styles/tokens.css` — one dark `:root` palette (20 colour tokens +
  `--font-mono`, `--cut`, `--chart-height`), `color-scheme: dark`.
- `frontend/src/styles/app.css` — rewritten HUD stylesheet (panel chamfer + corner
  brackets, primitives, header, strip, live, cost well, trend, summary, rate,
  diagnostics, skeleton, empty, responsive incl. the 1080×1920 grid, reduced motion).
- `frontend/src/App.vue` — shell in panel order; `StatusStrip` replaces both banners.
- `frontend/src/components/HudPanel.vue`, `StatusStrip.vue`, `ApiCostWell.vue`,
  `Gauge.vue`, `RatioRings.vue`, `Sparkline.vue`, `SegmentBar.vue`, `Donut.vue` — new.
- `frontend/src/components/{AppHeader,LivePanel,TrendPanel,SummaryCards,RatesPanel,DiagnosticsDisclosure,Card,SkeletonBlock,EmptyState}.vue`
  — rebuilt on the HUD frame; testids and rendered text preserved.
- `frontend/src/components/{ApiErrorBanner,OfflineBanner,ControlRow,LabelValue}.vue` —
  deleted.
- `frontend/src/lib/hudGeometry.ts` — new: arc/spark geometry, nice ceiling, cell
  fills, ratio tone bands.
- `frontend/src/lib/{live,format,chart}.ts` — sample buffer + peaks; `formatCompact`,
  `formatHistorySpan`; bar-only restyle returning `{ cleanup, resize }`.
- `frontend/src/main.ts`, `frontend/index.html` — theme bootstrap removed; `color-scheme` dark.
- `tests/test_ui_tokens.py` — single-palette token/contrast test.
- `scripts/smoke_frontend.py` — HUD gates (see §4), `live_slots()`, `spark_runs()`,
  `layout()` phase, `docs/receipts/hud-dashboard/` screenshots.
- `docs/specification.md` (D7), `docs/acceptance-scenarios.md` (scenario 31),
  `README.md` — documentation of the new surface.
