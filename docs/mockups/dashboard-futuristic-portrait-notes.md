# Futuristic portrait mockup — notes

File: `docs/mockups/dashboard-futuristic-portrait.html` (single file, 117,886 bytes, inline CSS + JS,
no CDN, no fonts, no network requests). Open it by double-click; it works from `file://`.

Everything on screen is **mock data generated in the page** (labelled `MOCK DATA` in the header).
Nothing under `frontend/` was modified. This is a visual-direction mockup, not a production change.

## How to drive it

A collapsible **DEV · states & ranges (mock)** tab (bottom-right) switches state, trend range, series and
the tick. The same is reachable by URL hash, which is also what the dev tab writes:

```
#state=offline&series=0&range=7d&open=diag,table&pause
```

| param | values |
| --- | --- |
| `state` | `live` · `starting` · `stale` · `offline` · `api-error` · `skeleton` · `empty` · `baseline` |
| `series` | `0` (engine 0, healthy) · `1` (engine 1, degraded/stale) |
| `range` | `24h` · `7d` · `30d` · `custom` |
| `from` / `to` | custom UTC day range (`YYYY-MM-DD`, end exclusive) |
| `open` | `table` · `req` · `diag` · `custom` (disclosures open at load) |
| `pause` | freeze the simulated tick |

Simulated cadence mirrors the real app: live row 1 Hz, panels every 15 s; under
`prefers-reduced-motion` the tick cadence becomes 15 s and all animation/transition is disabled.
Mock "now" is fixed at **2026-10-27 11:42:08 UTC** so screenshots are reproducible.

## Palette (Tokyo Night Storm) and every tweak

Stock values verified against `folke/tokyonight.nvim` `storm.lua`:
`bg #24283b`, `bg_dark #1f2335`, `bg_dark1 #1b1e2d`, `bg_highlight #292e42`, `terminal_black #414868`,
`border #1d202f`, `fg #c0caf5`, `fg_dark #a9b1d6`, `comment #565f89`, `fg_gutter #3b4261`,
`dark3 #545c7e`, `dark5 #737aa2`, `blue #7aa2f7`, `cyan #7dcfff`, `teal #73daca`, `green #9ece6a`,
`yellow #e0af68`, `orange #ff9e64`, `red #f7768e`, `magenta #bb9af7`.

| token | value | tweak vs stock | why |
| --- | --- | --- | --- |
| `--bg` | `#24283b` | none | page background |
| `--panel` | `#1f2335` | = `bg_dark` | panel face; the darker face is what makes the 1920-tall grid readable |
| `--panel-hi` | `#292e42` | = `bg_highlight` | nested cells (live cells, cards, log rows) |
| `--deep` | `#1b1e2d` | = `bg_dark1` | recessed chart bed / gauge bed |
| `--line` | `#3b4261` | = `fg_gutter` | 1px hairline borders (non-text, 1.9:1 — decorative) |
| `--bracket` | `#414868` | = `terminal_black` | HUD corner brackets |
| `--ghost` | `#2c3350` | **new**, between `fg_gutter` and `bg_highlight` | unfilled bar track + background grid; a touch lighter than `fg_gutter` so tracks read as tracks on `#1f2335` |
| `--muted` | `#9aa5ce` | **stock `comment` (#565f89) lifted** | `comment` is 2.51:1 on the panel face — far below AA. `#9aa5ce` keeps the same hue family at **6.41:1**. All secondary text uses this token |
| `--fg` / `--fg-dark` | `#c0caf5` / `#a9b1d6` | none | 9.63:1 / 7.37:1 on the panel face |
| `--ink` | `#1b1e2d` | = `bg_dark1` | text drawn **on** accent fills (e.g. request-slot chips): 11.9:1 on `--blue` |
| `--focus` | `#7dcfff` | = `cyan` | focus ring, 2px solid + offset (9.07:1) |
| `--series-input` / `--series-output` | `blue` / `orange` | mapping choice, colours stock | matches the real chart's `--series-input` / `--series-output` intent; blue/orange is the most separable pair in this palette and is colour-blind distinguishable |
| threshold ramp | `teal → yellow → red` | mapping choice, colours stock | Grafana-style zone colours, all ≥ 5.8:1 as text |
| `comment`, `dark3`, `dark5`, `fg_gutter` | stock | **kept, decorative only** | 2.4–3.7:1 → used for grid lines, ticks, ghost tracks, hairlines; never for text |

Contrast, measured on the rendered page (not from the tokens): every one of the 390 rendered text nodes
passes WCAG AA — worst pairs `#7aa2f7` 27px hero at 5.33:1, `#9aa5ce` 11–13px labels at 5.53:1.
SVG labels (gauge scale, chart axis, epoch timeline) are painted with `--muted`: they were
`--dark5` (`#737aa2`, 3.22:1 on `#292e42`) in the first build and were lifted — that was the only
contrast failure found.

## "Slightly futuristic" — what was used and what was avoided

Used: 1px hairline panel borders with **corner brackets** (8 background gradients, no extra elements);
a 34px background grid + faint scanline overlay (3px period, 10 % black at 1px); a low horizon glow at
the top of the page (blue at 10 %, radial); tabular monospace numerals
(`font-variant-numeric: tabular-nums`) with small uppercase letter-spaced panel labels, each marked by a
blue 3px tick and numbered (`01 Live engine activity`, `02 · Observed totals`, `03 · Trend`,
`04 · Latest rate`, `05 · Diagnostics`); semicircular arc gauges with a knob and threshold ring; a thin
glow **only** on the live dot, the glowing stat values (`.stat-value.glow`) and the gauge arc
(`.glow-arc`).

No CSS transitions are declared at all (hover states are instant colour changes on chips/buttons), which
is why the reduced-motion audit finds 0 transitions and 0 running animations.

Avoided: neon/multi-hue accents, blur/backdrop, scanline animation, animated gauges/sparklines,
animated background, gradient text. Under `prefers-reduced-motion` every animation and transition
becomes `0s` (verified: 0 running animations, 0 transitions, the live dot keeps a static glow).

## Metric → visualization

| data (real field) | visualization | exact text kept |
| --- | --- | --- |
| header status / badge | pulsing dot + badge (green Healthy, yellow Starting/Degraded, red Offline/Storage unavailable); blue bracket on the live panel when `state=live` | badge text, `Last successful update …`, series picker, theme toggle |
| `requests.active / queued` | stat + segmented slot bank (running/queued chips with counts) + 60-sample sparkline of active | `active / queued` + `running / waiting` |
| `prefill_tokens_per_second` | semicircular arc gauge with threshold bands + knob, big value, 60-sample sparkline | `N tok/s` + `instantaneous` |
| `decode_tokens_per_second` (+`_per_request`) | same gauge; per-request value as a secondary stat | `N tok/s · M/req` + `instantaneous · per running request` |
| `mtp_acceptance` 5s/1m/30m/lifetime | Grafana bar gauge, one row per window, threshold colour + ticks at warn/bad | `a / b / c / d` + `5s · 1m · 30m · lifetime` |
| `moe_gpu_hit` 5s/1m/30m | bar gauge rows | `a / b / c` + `5s · 1m · 30m` |
| ranges today / month / lifetime | stat panel: hero processed total, stacked input-vs-output ratio bar, rows for computed input / generated output / API-equivalent / cache hits (with meter), background sparkline of the last 24 hourly buckets | all labels, values, `Collecting since …`, baseline note, coverage note, cost flags, quality flag lines |
| trend history | tall full-width stacked **bar** chart (same bar semantics as the real Chart.js config): null bucket = red ×, never 0; incomplete last bucket hatched + dimmed; ghost grid; crosshair + readout (bucket, input, output, processed, quality) on hover, keyboard cursor (←/→/Home/End/Esc) with `aria-live`; legend; range chips + custom range; range errors as `role=status` | chart `aria-label`, `Last bucket is incomplete …` note, cost line, `Show as table` table with UTC bucket / computed input / generated output / processed total / quality |
| rates computed input / generated output | stat panel + mini bar gauge (share of scale) + sparkline of recent intervals + delta chip vs previous interval; dimmed with a warning indicator when stale | `formatRate` text, `over the last accepted interval · <UTC|no data>`, `Rates are unavailable while the series is stale` |
| request-accounted usage | disclosure with the note verbatim, request input/output token cells, observed-interval counts + gap meters | `REQUESTS_NOTE` verbatim, `<n> observed intervals · <gap>` |
| unobserved intervals | gap rows with a coverage meter each (engine counters, request input, request output, cached input) | `describeGap` text per counter |
| collection events | Grafana logs-panel style rows: severity stripe, kind, UTC time, JSON details, wrapped in `<code>` | `kind · UTC · JSON details`, `No collection events recorded.` |
| epochs | timeline strip: 30-day band, hatched gap hours, magenta epoch markers + reason list | `EPOCH_REASON` texts + `Opening balance — excluded from totals: N in, M out`, `No epoch boundaries recorded.` |
| quality flags | chips (union over ranges/rates/buckets + `metadata_incomplete`) | `FLAG_LABELS` text, `No quality flags on the current payload.` |
| raw retention / last error | two text blocks | `Raw samples are kept for N days.` + range + note, `Last collection error: <code>. …` |
| banners | offline banner (exact sentence), API error banner (+ `Showing data from …`) | verbatim |
| first load / empty | 3 skeleton blocks + chart block (`role=status "Loading dashboard"`); empty state sentence | verbatim |
| screen-reader line | visually hidden `role=status` | `<badge>. Last successful update <UTC>` / `No successful update yet.` |

## Portrait (1080 × 1920) layout decisions

* **One column, six full-width rows**: header → banners → Live → Observed totals → Trend → Latest rate →
  Diagnostics. Nothing primary is placed side by side; the freed height goes to the trend chart.
* Inside a row, panels tile horizontally with **container queries on `.shell`**, not viewport media
  queries (5 live cells in one row, 3 summary cards in one row). This is what makes the portrait layout
  survive: at 1080 CSS px wide the shell is only 1048 px, and viewport breakpoints tuned for a 1100 px
  desktop used to collapse it to 3+2 wrapped rows, doubling the row height and crushing the chart.
* **Fit mode** (`min-width 900px and min-height 1500px and orientation: portrait`): the shell becomes a
  `100dvh` grid with rows `auto auto auto minmax(0,1fr)` — the trend panel absorbs the leftover height,
  so the chart stretches instead of leaving a gap. Measured row bottoms: header 151, live 458,
  summary 1058, trend 1549 (chart 260 px tall), rates 1775, diagnostics header 1848 → everything is
  visible with ~72 px to spare, and the **document** never scrolls; the shell does, for expanded
  disclosures.
* Fit-mode trims (measured, not guessed): `--fs-hero` 36, `--fs-stat` 27, `--gap` 11, panel padding
  12/14, cell padding 8/10, gauge max-height 74, sparkline 24, card padding 9/11, notes/flags 11px.
  These are the smallest values that kept every label at ≥ 5.5:1 and readable at 11–12px in a
  screenshot pixel audit.
* Landscape 1920×1080: the shell caps at 1560 px, centered, chart 380 px, normal page scroll.
  ~768 px: live cells 2-up then 1-up, summary cards 1-up, header stacked, `--fs-hero` 32 / `--fs-stat` 23.

## Assumptions (labels, scales and thresholds are not defined by the app)

The API returns raw token counts and ratios; nothing in `frontend/` defines gauge maxima or zone
thresholds. **All of the following are mockup assumptions**, chosen to be plausible for a
quantized 30B-class engine with MTP and MoE GPU-offload:

* Gauge full scales: prefill **4 000 tok/s**, decode **300 tok/s**, latest rate input **6 000 tok/s**,
  output **200 tok/s**, request slots **12 running / 6 queued**.
* Saturation zones (gauges, mini gauges): teal below **62 %** of scale, yellow 62–85 %, red above 85 %.
* MTP accept: red below **50 %**, yellow 50–70 %, teal ≥ 70 %.
* MoE GPU hit: red below **65 %**, yellow 65–85 %, teal ≥ 85 %.
* Mock live values: prefill ~1 240 tok/s, decode ~180 tok/s, ~60 tok/s per request, MTP 70 %/– / –/66 %,
  MoE 93 %/91 %/–, 3 running / 1 waiting (same shape as the live-plan example payload).
* Mock volume: ~4.2 M computed input tokens per hour with a daily/weekend curve; costs from
  `cost_rates` 1.25 / 0.25 / 6.00 USD per Mtok (input / cached input / output); cache hit ratio ~55 %.
* Mock gaps/events: 4 unobserved hours (one full no-data hour), 8 collection events using only real
  `accounting.py` kinds, 3 epochs (`new_series`, `clock_regression`, `restart_evidence`),
  `raw_retention_days` 90, `connect_timeout` as the last error code, `network_error` for the API banner.
* The two series (`engine 0` healthy, `engine 1` degraded with `stale_since`) exist only to exercise the
  series picker; the picker is rendered only when `series.length > 1`, exactly as in the real header.

## Current-UI data not represented, and behavioural differences

* **Light and system themes are not implemented.** The theme button cycles `system → light → dark`
  labels and carries a tooltip saying the mockup is dark-only; the real control switches themes via
  `localStorage["jarvis-theme"]`.
* No real backend: the mock synthesises the shapes from `openapi.json` (`live`, `summary`, `history`,
  `events`, `status`, `series`). Fields the current UI never renders (`poll_interval_seconds`,
  `series_count`, `shutting_down`, `source_id`, `collector_code`, `storage_code`, `server_time`) are
  therefore absent, as in the current UI.
* `epochs.next_cursor` (API pagination) has no UI here — the current UI has none either; the mock shows
  3 epochs.
* The chart tooltip is hover + keyboard only; on touch, the `Show as table` disclosure is the path
  (same fallback the current UI offers).
* The real app re-fetches on `visibilitychange`; the mock re-renders, and the tick pauses when the tab
  is hidden and with `#pause`.

## Verification

37 automated checks driven over CDP against headless Chromium (session-local scripts: `cdp.mjs`,
`verify.mjs`, `pngaudit.mjs` in `/tmp` — not part of the deliverable; report `verify-report.json`),
all passing on the shipped file:

* **Offline + clean**: loaded from `file://` — 0 console errors, 0 warnings, 0 `Log` entries, and **0
  network requests** beyond the document itself. Source-level cross-check: the file contains 0
  occurrences of `src=`, `<link`, `@import`, `url(http`, `fetch(`, `XMLHttpRequest`.
* **Fit at 1080×1920** (disclosures collapsed): no horizontal overflow, no clipped panel, document does
  not scroll, trend bottom 1549 and rates bottom 1775 ≤ 1920, chart drawn. All 8 states re-checked:
  none scrolls the document, none overflows.
* **Contrast**: 390 rendered text nodes audited (HTML computed styles + SVG fills, alpha composited over
  the real background stack) — 0 AA failures; the lowest ratio anywhere on the page is 5.33:1
  (`#7aa2f7` 27px hero value), secondary labels sit at 5.53:1.
* **Keyboard**: 7 real `Tab` presses reach the chart (`outline: rgb(125,207,255) 2px solid`), 5 reach a
  dev chip; ←/→/Home/End move a crosshair and write the readout + `aria-live` announcement, Esc clears.
* **Data paths**: 7d → 7 day buckets, 30d → 30 (matches `dashboard.ts`), custom 16 buckets,
  > 366 days → `Narrow the range to 366 days or fewer.`, diagnostics shows 8 events / 3 epochs /
  4 gap rows / timeline, request-accounted note present, expanded disclosures scroll inside the shell
  only.
* **States**: offline banner sentence exact; API error banner + `Showing data from …`; stale rates show
  `N/A` + the stale notice; skeleton; empty sentence; baseline note; `starting` renders every live value
  as `-`.
* **Reduced motion**: with `prefers-reduced-motion: reduce` emulated — 0 running animations, 0
  transitions, pulse duration 0s.
* **Live simulation**: live row content changes every second (5 sparklines, 2 gauges).
* **Deep link**: `#state=offline&open=diag,table&range=7d` restores state, disclosures and range.
* 1920×1080 and 768×1024: no horizontal overflow, no clipping, chart drawn.

Screenshots in `docs/mockups/` (all from the shipped file):

| file | content |
| --- | --- |
| `screenshot-portrait-1080x1920-live.png` | primary: Live at 1080×1920 |
| `screenshot-portrait-1080x1920-offline.png` | degraded: offline banner, stale live row, dimmed rates |
| `screenshot-portrait-1080x1920-stale.png` | degraded: stale chip + rates notice |
| `screenshot-portrait-1080x1920-expanded.png` | trend table + diagnostics expanded (shell scrolls) |
| `screenshot-portrait-1080x1920-starting.png`, `-empty.png` | first-run shapes |
| `screenshot-landscape-1920x1080-live.png`, `-offline.png` | landscape |
| `screenshot-tablet-768x1024-live.png` | narrow |

**Not verified, honestly:** this session's model cannot open images, so no human-eye visual review of
the screenshots took place. As a substitute the PNGs were decoded and measured (session-local
`pngaudit.mjs`): every panel region contains ink (header 7.6 %, live row 77 %, summary 12.9 %,
chart 15.6 %, rates 38.9 %), the colour census is dominated by the Tokyo Night surfaces
(`#1f2335` 38 %, `#1b1e2d` 21 %, `#292e42` 12 %, `#24283b` 8 %), the trend chart contains both series
colours (27 451 blue / 5 285 orange px) and the gauge region contains the teal value arc plus the amber
and red zone bands. That measures presence, density and palette, not taste: **subjective visual polish
still needs a human look.**
