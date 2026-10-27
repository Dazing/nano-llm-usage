# Retro instrument-panel mockup — notes

File: `docs/mockups/dashboard-retro-tokyonight.html` (single file, inline CSS/JS, no network, all data is **MOCK DATA**).
Open it by double-click. Dev switcher: small `DEV` tab, bottom-right (or URL hash, e.g. `#state=stale`, `#state=offline&open=diag,req`, `#pause`, `#series=1`, `#range=7d`).
States: Live, Starting, Stale, Offline banner, API error, First-load skeleton, Empty, Baseline only (extra: real UI's "Collecting a first baseline" state).

Screenshots (this folder, `screenshot-*.png`): desktop 1440 live / stale / offline + diagnostics open / API error / starting / skeleton / empty; tablet 768 live / stale.

## Palette

Stock Tokyo Night Storm values are kept for backgrounds, text and all eight accents. Tweaks:

| Token | Value | Change from stock | Reason |
|---|---|---|---|
| `--page` `--page-dark` `--highlight` `--terminal-black` | `#24283b` `#1f2335` `#292e42` `#414868` | none | |
| `--fg` / `--fg-dark` | `#c0caf5` / `#a9b1d6` | none | |
| `--muted` (text) | `#9aa5ce` | stock `comment` `#565f89` lifted | `#565f89` is 1.9:1 on the faceplate; `#9aa5ce` is 4.95:1. Stock comment kept only for decorative ticks/ghost lines |
| `--panel` | `#2f3550` | new (between `bg_highlight` and `terminal_black`) | slate faceplate that separates from `--page` and keeps text ≥ 4.9:1 |
| `--panel-hi` / `--module` | `#3b4261` / `#2a3047` | `fg_gutter` / new | bezel highlight / nested module face |
| `--outline` | `#13141c` | darker than stock `#15161e` | thick hardware outline needs more separation |
| `--screen` | `#16161e` | ≈ stock `#15161e` | recessed display |
| `--ghost` | `#262b40` | new | unlit lamp / 7-segment (decorative) |
| `--cream` / `--ink` | `#b4bde4` / `#1a1b26` | new (cool "cream") | VU dial faces and label strips (reference's cream), ink on cream 9.9:1 |
| Dial zones on cream | `#2a8f80` `#b07a22` `#c23a56` | darkened stock teal/yellow/red | stock accents wash out on the light dial |

Measured: 377 rendered text nodes (computed fg/bg, SVG text via `fill`) → lowest ratio 4.95:1. Non-text contrast was checked by eye only.

## Metric → instrument

| Real data | Instrument (number is always real text) |
|---|---|
| Header: collection status / storage | status lamp (green Healthy, amber Collecting/Degraded, red Offline/Storage unavailable); series name on an LCD strip; series picker = native `<select>` + rotary knob; theme = rocker; `MOCK DATA` plate |
| Live chip | lamp: Live green, Starting amber, Stale red |
| Requests running / waiting | 7-segment readout + two 10-lamp banks (green / amber) |
| Prefill, Decode (+ per request) | semicircular needle gauge with zoned arc, LCD value, 60 s sparkline |
| MTP accept 5s/1m/30m/lifetime; MoE 5s/1m/30m | 20-segment LED bar per window, red/amber/green zones, `%` text; null = `-` and empty bar |
| Summary cards (today / month / lifetime) | 7-segment Processed total + sparkline, LCD input/output, stacked input-vs-output ratio bar, API-equivalent, cache-hit fill meter, flag lamps + text |
| Trend | oscilloscope: phosphor graticule, stacked bars (blue input, orange output), hatched incomplete bucket, `×` marker for unobserved bucket (never a zero bar), amber marker for partial; keycap range keys 24h/7d/30d + Custom (UTC date range with the real validation messages); cursor readout (mouse, click, ←/→/Home/End); "Show as table" disclosure |
| Latest rate | cream VU meters, LCD value, cream label strip with observed-at; stale: dimmed, N/A, amber lamp + notice |
| Request-accounted usage | disclosure, LCD tokens, intervals and gap text |
| Diagnostics | fold-out: gap lamps, quality-flag lamps, green-phosphor event tape, epoch timeline strip (gaps hatched) + opening balances, retention, last error lamp |

## Not representable / deviations

- **Theme toggle**: the real one cycles system/light/dark. Mockup is dark-only, so the rocker is shown `aria-disabled`.
- **Live cadence**: real live row polls at 1 Hz and the rest at 15 s (not 15 s for everything). Mockup ticks live at 1 s, slow panels every 15 s of simulated time; under `prefers-reduced-motion` the tick is 15 s and all transitions/animations are off.
- **Live sparklines** need a client-side ring buffer of the last 60 live polls (no new API field). **Summary sparklines** (hourly today, daily month/lifetime) need extra `/history` reads that the current page does not make; they would compete for the reader budget the current code is careful about.
- Input/output percentage in the ratio bar is derived client-side.
- API-equivalent and cost footnote shown only for the "rates configured" case (real UI hides them otherwise).
- Y-axis uses compact units (M/B); the real Chart.js axis shows raw numbers.
- Event kinds come from `src/jarvis_dashboard/accounting.py`; the error code `connect_timeout` from `metrics.py`.

## Assumptions (mock values, not from the API)

- Gauge full scale: prefill 12,000 tok/s, decode 300 tok/s; VU full scale: computed input 8,000 tok/s, generated output 300 tok/s. Zones: 0–62 % teal, 62–85 % amber, 85–100 % red (saturation bands).
- Ratio thresholds: MTP red < 50 %, amber 50–70 %, green ≥ 70 %; MoE red < 65 %, amber 65–85 %, green ≥ 85 %.
- Lamp banks hold 10; no real maximum concurrency is known.
- Mock "now" is fixed at 2026-10-27 11:42:08 UTC; series, gaps, epochs and token volumes are synthetic but self-consistent (today ⊂ month ⊂ lifetime come from the same hourly data).

## Verification

Done (Chromium headless 155, `file://`): no console errors, only the document request, no horizontal page scroll at 1440 and 768, range keys / custom range (valid + invalid) / series switch / keyboard cursor on the scope / focus return, live tick moves needles, reduced-motion disables animation and slows the tick.
Not done: Firefox/Safari, touch devices, screen-reader run, an automated WCAG tool, widths below 768 (a narrow-width rule exists but was not reviewed).
