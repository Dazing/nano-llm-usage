# Jarvis HUD mockup — handoff report

**Deliverable:** `frontend/mockups/hud-dashboard-qwen.html` (95 KB, single file, inline CSS + JS + SVG, zero network
requests). Open it directly: `file:///…/frontend/mockups/hud-dashboard-qwen.html`.
Not added to the Vite build (`frontend/mockups/` is outside `src/`, untracked, no imports).

**Superseded draft in the same folder:** `frontend/mockups/hud-dashboard.html` + `screenshots/` are an earlier pass
from this session, kept for comparison. `hud-dashboard-qwen.html` + `shots/` are the verified deliverable.

## How to review

| Action | How |
| --- | --- |
| Live animation | runs on load, one tick every **1.2 s** (prefill/decode gauges, request counts, MTP/MoE rings, sparklines) |
| State toggle | header **`STATE ▾`** button, or press **`s`**, or `#state=live|starting|stale|offline|error|loading|empty` |
| Trend range | `24H · hourly` / `7D` / `30D` / `CUSTOM · UTC` (date inputs + Apply, with the app's real range errors) |
| Chart tooltip | hover any bucket; `#tip=1` opens it programmatically for a screenshot |
| Table view | `Show as table` disclosure replaces the plot (scrolls inside the panel); `#table=1` for a screenshot |
| Series selector | header select (3 series); switches model/engine, units, and every panel's data |
| Layout audit | `#debug=1` paints a bottom strip: `AUDIT OK · 1080×1920 · no scroll · state=…` |
| Headless self-test | `#test=1` runs 50 assertions, sets `document.title` to `ALLPASS` / `TESTFAIL n` |

## Reference traits applied

**Caveat, stated plainly:** the three reference URLs downloaded fine (valid JPEGs, 1200×675, 1200×675, 626×626),
but this session's model cannot render images — every attempt to view them returned
`[Current model does not support images]`, and `fetch_content` rejects `image/jpg`. So the traits below were
**measured from the pixels** (ImageMagick statistics on the downloaded files), not seen. They are objective
measurements, not an aesthetic reading; a human eye should confirm the qualitative judgement.

| # | Measured in the references | Applied in the mockup |
| --- | --- | --- |
| 1 | Near-black ground: mean luminance 0.085 / 0.099 / 0.201, 74–84 % of pixels below 12 % luminance, bright pixels ≤ 2.3 % | Page ground `#10131f`, panels a 2-stop dark gradient; hierarchy by luminance — the brightest pixels are the key numbers, decoration stays ≤ 40 % luminance |
| 2 | Sparse thin line work: edge density 2.6–4.2 % | 1px panel borders, 16px corner ticks, hairline rules after each panel title, 1px gridlines |
| 3 | One dominant accent family per screen (amber 86 % / red-magenta 94 % / cyan 77 % + 23 % warm) | Dominant cool family (blue/cyan) for structure and input; **one** warm accent (orange) reserved for generated output and alerts — mirrors the app's `--series-input` / `--series-output` |
| 4 | Horizontal banding autocorrelation 0.65 @ lag 2, 0.47 @ lag 3 (≈5 px grid) | 5 px scanline overlay + faint grid + vignette over the whole screen (`#hud::after`) |
| 5 | Brightness weighted to the top-left (0.359) falling to the bottom-right (0.112) → framing at the edges | Angled (clip-path) panel corners, bracket ticks at panel corners, a sweeping scan band across the top of the live panel |
| 6 | Content spread over all nine 3×3 zones — dense multi-panel, no single hero | Seven stacked rows filling 1080×1920 with no hero panel; the live gauges are the brightest zone, not the largest |

## Final palette

| Token | Hex | Role | WCAG on panel `#1a1e30` |
| --- | --- | --- | --- |
| `--bg-deep` | `#10131f` | page ground | — (adjusted) |
| `--pnl-a` / `--pnl-b` | `#1c2135` / `#161a2b` | panel gradient | — (derived from `bg_dark`) |
| `--sunk` | `#121627` | inset wells, chart ground | — |
| `--fg` | `#c0caf5` | primary text/numbers | 10.2:1 |
| `--mute` | `#8892b8` | **all** secondary text | 5.38:1 (adjusted) |
| `--line` | `#565f89` | decorative line/grid work only | 2.5:1 — never text |
| `--blue` `--cyan` `--magenta` `--green` `--teal` `--yellow` `--orange` `--red` | `#7aa2f7` `#7dcfff` `#bb9af7` `#9ece6a` `#73daca` `#e0af68` `#ff9e64` `#f7768e` | accents, unchanged from the brief | 6.55 · 9.62 · 7.14 · 9.03 · 9.91 · 8.25 · 8.12 · 6.24 |
| semantic | `--in`=blue, `--out`=orange, `--cost`=yellow, `--mtp`=magenta, `--moe`=cyan, `--cache`=teal, `--ok/warn/bad`=green/yellow/red | metric identity, used consistently everywhere | as above |

Two adjustments and why:

1. **`bg_dark #1f2335` → `#10131f` page + `#1c2135`/`#161a2b` panels.** The references sit near black; at `#1f2335`
   the screen reads as a flat card dashboard and the glowing accents lose contrast. Every accent clears 5:1 on
   `#10131f` too.
2. **`comment #565f89` → `#8892b8` for secondary text.** `#565f89` measures 2.5:1 on `#1f2335` (fails 4.5:1 for the
   12–13 px labels a HUD needs). The lifted value measures 5.38:1 on panels / 5.07:1 on `#1f2335`; `#565f89` is kept
   strictly for non-text line work, where contrast requirements do not apply.

## Panel → visualization mapping

| Original panel / field (from `frontend/src/components/`) | HUD treatment |
| --- | --- |
| `AppHeader.vue` title, `model_name · engine N` | brand block, mono, brightest text in the header row |
| series selector (`>1` series) | native `<select>` in the header; repaints every panel |
| status badge (storage / collector) | bordered chip, `color:currentColor` + text label (never colour alone) |
| last successful update | labelled well `Last successful update · UTC` |
| `SIMULATED` | header tag + top-of-file code comment + `MOCK` section header |
| Offline / API-error banners + `role="status"` live line | one banner strip (`#p-banner`) with icon glyph, text, and the SR line kept as a real `aria-live="polite"` region |
| live chip Live / Stale / Starting + `observed_at` | pulsing dot chip (animation pauses on non-live states) + `observed <UTC>` |
| requests running / waiting | big `N / M` number + two 16-segment meters (`ACTIVE`, `WAITING`) |
| prefill tok/s (instantaneous) | 180° arc gauge, threshold-coloured, tick marks, big centre number, 40-sample sparkline |
| decode tok/s + `per running request` | second arc gauge + `/req` sub-value + sparkline |
| MTP acceptance 5s · 1m · 30m · lifetime | **four concentric rings** + four 10-segment bars with the numeric ratio beside each |
| MoE GPU hit 5s · 1m · 30m | **three concentric rings** + three segmented bars, same pattern |
| summary cards (today / month / lifetime) | three cards, each: big processed total, 2-bar input/output split with both numbers, **cache-hit donut** + `fmtCacheShare` text, cost value (only when `cost_rates` set, else `cost rates not configured`), coverage text, quality flags as text chips, `Collecting since <date>` on lifetime |
| cost-rates footnote | one line under the cards, verbatim semantics |
| trend 24h/7d/30d + custom UTC | SVG stacked bars (input blue, output orange) + right-axis dashed cost line, gridlines, both axis label sets, legend, hover tooltip with all five fields + quality + gap |
| "last bucket is incomplete" note | note chip next to the notes row |
| `Show as table` disclosure + Quality column | replaces the plot in place, scrollable, per-bucket `Quality` column |
| range errors (`invalid_time`, `range_too_large`, missing dates) | red text line in the notes row, app wording |
| latest rate panel (computed input / generated output) | two threshold-coloured numbers + 24-segment peak-scaled meters + `over the last accepted interval · <UTC>` + tokens/interval seconds |
| rates stale notice | inline warn text |
| Request-accounted usage disclosure | `<details>` with request input/output, observed intervals, gap description |
| `DiagnosticsDisclosure.vue` | `<details>` in the bottom strip; meta line carries counts when collapsed; sections: unobserved intervals (4 scopes), collection events, epochs with opening balance, quality-flag union, raw retention + last error |
| loading skeleton | shimmer grid over the live/trend/summary/rates slots |
| empty state | centred panel with the app's copy and the "collecting since" hint |

## Omissions (deliberate)

- **Theme toggle** (`system / light / dark` in `AppHeader.vue`): the brief fixes a dark Tokyo Night Storm HUD; a
  light theme is a second design, not a variant of this one.
- **Responsive behaviour:** out of scope per the brief; the layout is a fixed 1080×1920 canvas.
- **Production accessibility audit:** the mockup keeps real `<button>`, `<select>`, `<details>`, focus rings, an
  `aria-live` region, text-labelled flags, and a `prefers-reduced-motion` block that stops all animation, but it was
  not audited to WCAG.
- Nothing from `frontend/src/components/` is otherwise dropped; every field listed in the mapping renders from the
  simulated fixtures.

## Verification (all automated, headless Chromium)

`chromium --headless=new --window-size=1080,1920 --virtual-time-budget=25000 --dump-dom "…#test=1&debug=1"` →
**50 / 50 PASS, 0 console/JS errors** (`document.title = ALLPASS`). Checks include: page exactly 1080×1920 with no
scrollbars; **no element clipped by a panel's `overflow:hidden`** (per-element rect vs nearest clipping ancestor);
no text node overflowing its own box; per-panel `scrollHeight ≤ box` (92/44/454/432/452/212/158 + 10 px gaps +
16 px padding = 1920); gauges/rings/segments/bar/section counts; tooltip content; 24h→24 hourly, 7d→7 daily,
30d→30 daily buckets; custom 10-day range → 10 buckets; `>366 d` → "Narrow the range to 366 days or fewer."; empty
dates → "Choose valid UTC dates for the range."; 24 table rows; series switch; request-accounted text; the live loop
changing values between ticks; all seven states rendering with the right chip and no scroll; no `undefined`/`NaN`
text.

Screenshots in `frontend/mockups/shots/` (all measured 1080×1920):
`hud-state-live.png`, `hud-state-starting.png`, `hud-state-stale.png`, `hud-state-offline.png`, `hud-state-error.png`,
`hud-state-loading.png`, `hud-state-empty.png`, `hud-tooltip-24h.png`, `hud-trend-30d.png`, `hud-table-view.png`,
`hud-audit-overlay.png`.

**Not verifiable here:** the appearance of the screenshots and of the three reference images. This model cannot
decode images, so no human-level visual review happened. Substitutes: (a) geometry/overflow/clipping assertions
above, (b) per-band pixel statistics on the screenshots — every panel band has thousands of unique colours and
non-trivial standard deviation (header 0.128, banner 0.088, live 0.108, trend 0.194, summary 0.156, rates 0.125,
diagnostics 0.058 — the low value is the collapsed disclosure), and (c) a colour histogram matching the palette
(`#15192A` ground, `#29314A` panels, `#7399E9` blue, `#D4A363` orange, `#637093` muted, `#A1AAD2` foreground).
Contrast was computed analytically (WCAG formulas), not sampled from the render. A human should eyeball the shots
before this direction is approved.
