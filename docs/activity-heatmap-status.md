# Year activity heatmap — implementation evidence

Receipts: [`docs/receipts/activity-heatmap/`](receipts/activity-heatmap/). Plan:
[activity-heatmap-implementation-plan.md](activity-heatmap-implementation-plan.md).
Date: 2026-10-27 (UTC). Contract: specification
[D7](specification.md#d7-dashboard-design-contract), scenarios
[31](acceptance-scenarios.md#d-transactions-and-recovery) and
[47–49](acceptance-scenarios.md#h-year-activity-heatmap-specification-d7).

## 1. Summary

- **Outcome:** the panel is implemented end to end — a pure window/scale model
  (`frontend/src/lib/heatmap.ts`), a fifth store slot with its own 300 s cadence inside
  the existing refresh (`frontend/src/lib/dashboard.ts`), the `Activity` panel as head
  `03` between Trend and Period summary (`frontend/src/components/ActivityPanel.vue`,
  `frontend/src/App.vue`, panels after it renumbered 04/05/06), its styles plus the
  portrait row budget (`frontend/src/styles/app.css`), and two new browser phases
  (`activity_live`, `activity_seeded` in `scripts/smoke_frontend.py`).
- **Nothing behind it moved:** no API, query, schema, OpenAPI or `MAX_BUCKETS` change
  (`export_openapi.py --check` exit 0; the full Python suite, 374 tests, passes), and the
  panel adds no timer — the browser phase still reports `poll_timers: [1000, 15000]` and
  `activity_adds_no_timer_ac5: '15000,1000'`.
- **Window arithmetic is exact.** On the execution day (2026-10-27, Tuesday,
  `getUTCDay = 2`) the grid drew **360 cells = 358 + 2** in **52 columns × 7 rows**, from
  `2025-11-02T00:00:00.000Z` to `2026-10-28T00:00:00.000Z`: one
  `/api/v1/history?bucket=day` request (`activity_one_window_request_ac1: 1`), half-open,
  360 days — inside the API's 366-bucket cap.
- **Against a seeded year, every drawn state matched the API for every day.** 360 days
  compared cell by cell: `activity_seeded_states_match_the_api_ac2: []` and
  `activity_seeded_partial_matches_the_api_ac3: []`. The maximum day (`2026-10-16`,
  609 196 processed tokens) rendered `('1.000', '100.0')`; the worst deviations over the
  whole window were **ratio 0.000000** and **pct 0.047162** against the contract formula
  `20 + 80·√ratio` (tolerance ±0.1 of fill percent).
- **Two production defects were caught by the verification path** — the seeder could not
  seed at all (`KeyError: 'cached_input'`), and the panel hid the reason a day is hollow.
  Both are in §6.
- **Working-tree interference:** the suite aborts on this checkout at a *pre-existing*
  gate, `dom_utc_labelled`, because an uncommitted change that is not part of this task
  removed the card's `UTC` marker. The green run in §4 was taken with that marker
  restored, and the file was put back byte-exact afterwards (§7).
- **Not claimed:** the D9 resource workload with this read in it (scenario 33) was not
  executed, and no real Radiance engine was contacted (§8).

## 2. Environment (executed gates)

| Field | Value |
|---|---|
| OS / kernel | `Linux 7.2.11-1-cachyos x86_64` |
| Python | 3.13.17 (`.venv`); `PYTHONPATH=src` is required for `python -m jarvis_dashboard…` on this host (the editable-install `.pth` points at a stale path; the `scripts/*` harnesses and `pytest` set it themselves) |
| Node / npm | v24.22.0 / 11.19.1 |
| Browser | Chromium 156.0.8078.47 (Debian 12 build), headless, driven over CDP by `scripts/browser_driver.py` |
| Docker / compose | absent — image gates NOT EXECUTED (§8) |
| Git | HEAD `1769b03` on `main`; 38 tracked files changed (+690/−45) plus new paths: `frontend/src/lib/heatmap.ts`, `frontend/src/components/ActivityPanel.vue`, this status document, `docs/receipts/activity-heatmap/`, and the disposable probes of §5 |
| Clock | `date -u` = `Tue Oct 27 21:49 UTC 2026` — the container clock is set to 2026, so "today" in every receipt is 2026-10-27 |
| Upstream | `tests/fixtures/fixture_server.py` (loopback fixture) and `scripts/seed_history.py` (local SQLite seed). The real Radiance engine was **not** contacted. |

## 3. Acceptance criteria (plan AC1–AC8)

Check names are the `check(...)` labels printed by `scripts/smoke_frontend.py` in the run
of §4; `phase 6b` is the live-fixture activity phase, `phase 6c` the seeded-year one.

| AC | Requirement | Evidence | Result |
|---|---|---|---|
| AC1 | `activity-panel` is head `03` between `trend-panel` and `summary-panel`; 52 × 7; first column a Sunday; no future day; cell count `358 + getUTCDay(today)`; exactly one day-bucket request for that half-open range | `panel_order: ['app-header', 'live-panel', 'trend-panel', 'activity-panel', 'summary-panel', 'rates-panel', 'diagnostics-panel']`, `activity_one_window_request_ac1: 1`, `activity_window_is_half_open_ac1: True` (the request's `from`/`to` equal the model's window), `activity_cell_count_ac1: 360` and `activity_seeded_cell_count_ac1: 360` (= 358 + 2), `activity_weekly_columns_ac1: 52` / `activity_weekday_rows_ac1: 7` / `activity_seeded_columns_ac1: 52`; window arithmetic (Sunday start, no future cell, `days ≤ 366`) pinned for five dates — including 2024-02-29, 2026-01-01 and a Sunday — by `frontend/probe_heatmap.ts` | pass |
| AC2 | null → `none` hollow; `"0"` → `zero`; positive → `value` with `data-ratio` (3 dp) and `data-pct = 20 + 80·√ratio` (1 dp); max day `1.000 / 100.0`; a quarter of max `60.0`; a null day is never drawn like a zero | `activity_seeded_states_match_the_api_ac2: []` (all 360 days, each cell compared with the API's own day bucket), `activity_seeded_max_is_one_hundred_ac2: ('1.000', '100.0')` on `2026-10-16` (609 196 tokens), `activity_quarter_day = 2026-09-16 ratio 0.4758` → `activity_seeded_quarter_is_sixty_ac2: True`, `activity_worst_ratio_deviation = 0.0` + `activity_seeded_ratios_within_tolerance_ac2: True`, `activity_worst_pct_deviation = 0.047162` + `activity_seeded_scale_within_tolerance_ac2: True`, `activity_seeded_legend_ac2: 'max 609.2 K on 2026-10-16'`, `activity_seeded_idle_day_is_zero_ac2: 'zero'` (2026-10-22, the seeded idle day) against `activity_seeded_downtime_day_is_hollow_ac2: 'none'` (2026-10-15) and `activity_seeded_day_before_the_seed_is_hollow_ac2: 'none'`; live fixture: `activity_unobserved_days_stay_hollow_ac2: []`, `activity_today_is_the_value_ac2: 'value'`, `activity_today_is_the_max_ac2: ('1.000', '100.0')`, `activity_value_cell_carries_the_fill_share_ac7: True` (the computed fill of the max cell is the accent, of a 20 % cell the sunk colour); unit level: the exact-string maximum (20 000 000 000 000 001 beats 2×10¹⁶, which `Number()` cannot separate), the 20.0 % floor at one token, `2^53+1` preserved | pass |
| AC3 | `partial` / `incomplete` (always including today) get the `--warning` outline and `data-partial="true"`; every tooltip states the UTC date, the exact total (or "no data"), the percent of max and the quality | `activity_seeded_partial_matches_the_api_ac3: []` (the flag on all 360 cells equals `partial ∥ incomplete ∥ day == today`), `activity_seeded_today_is_partial_ac3: 'true'`, `activity_today_is_partial_ac3: 'true'`, `activity_partial_is_outlined_ac3: True` (computed stroke of `.hm-partial` is `--warning`), `activity_tooltip_today = 2026-10-27 · 974 · 100% of max · collection_gap · time_attribution_approximate · partial`, `activity_tooltip_max = 2026-10-16 · 609,196 · 100% of max · collection_gap · time_attribution_approximate · partial`, `activity_tooltip_exact_total_ac3: True`, `activity_tooltip_quality_ac3: True`, `activity_tooltip_names_the_share_ac3: True`, `activity_tooltip_hollow = 2026-08-25 · no data · — · complete` + `activity_tooltip_of_a_hollow_day_ac3: True` | pass |
| AC4 | `aria-label` summary (window, max value and date, no-data count); `activity-disclosure` table with one row per day (date, processed, computed input, generated output, quality); focus order after `chart-disclosure` | `activity_grid_role_ac4: 'img'`, `activity_aria_names_the_window_ac4: True`, `activity_aria_counts_the_hollow_days_ac4: True`, `activity_aria_names_the_max_ac4: True`, `activity_seeded_aria_names_the_max_ac4: True` (`Busiest day 609,196 on 2026-10-16.`), `activity_seeded_aria_counts_hollow_ac4: True`, `activity_table_row_per_day_ac4: 360`, `activity_table_last_row_is_today_ac4: '2026-10-27'`, `activity_table_row_carries_the_api_total_ac4: '974'` (the rendered row equals `GET /api/v1/summary` for the same day), `activity_seeded_table_rows_ac4: 360`, and four seeded rows compared field by field with `GET /api/v1/history`: `2026-10-16 → ['609,196', '568,845', '40,351', 'collection_gap · time_attribution_approximate · partial']`, `2026-10-22 → ['0', '0', '0', 'time_attribution_approximate']`, `2026-10-15 → ['no data', 'no data', 'no data', 'collection_gap · partial']`, `2026-08-25 → ['no data', 'no data', 'no data', 'complete']`; `keyboard_focus_sequence = …, 'chart-disclosure', 'activity-disclosure', 'requests-disclosure', …` with `keyboard_focus_covers_the_contract` and `keyboard_focus_order: True` | pass |
| AC5 | Read on first load, on a series change, when the UTC window start changes, otherwise at most once per 300 s; sequential inside the existing cycle; timers stay `[1000, 15000]`; a hidden tab makes no reads; a stale-series response is discarded; a failed read keeps the year on screen | `activity_waits_out_its_cadence_ac5: 1` (two full 15 s poll cycles plus 8 s → still exactly one day-bucket request), `activity_adds_no_timer_ac5: '15000,1000'` and `poll_timers: [1000, 15000]`, `polls_paused_when_hidden: 212 (expected 212)` with `poll_timers_cleared_when_hidden: 0` (the activity read rides `refresh()`, so the hidden-tab gate covers it), `poll_resumes: True`, `activity_discards_the_stale_series_ac5: '0'` (the first series' day-window read is delayed 2.5 s, the picker is switched mid-flight, and every rendered cell for the old series is gone), `activity_switch_rereads_the_window_ac5: 1`; phase 7d `api_failure_activity_grid_present: True` and `api_failure_activity_kept: True` (the 360 rendered cells are byte-identical after the dashboard process is killed — the shared stale notice carries the failure, the grid never empties); unit level: `frontend/probe_cadence.ts` (11 checks) pins `activityDue` on first load / series change / window change / 299 s vs 300 s | pass |
| AC6 | 1080×1920: eight panels fill the screen with no page scroll and the grid fully visible; 1440: vertical scroll only; 390: no horizontal page scroll, the grid scrolls inside the panel starting at the current week | `activity_portrait_needs_no_inner_scroll_ac6: True` (the grid scroller's `scrollWidth ≤ clientWidth` — all 52 weeks are inside the panel), `activity_portrait_page_does_not_scroll_ac6: True` (`documentElement.scrollHeight ≤ innerHeight + 1`), `portrait_panels_without_nominal_bar: 7`, `activity_mobile_page_has_no_side_scroll_ac6: True`, `activity_mobile_grid_scrolls_inside_ac6: True`, `activity_mobile_grid_opens_on_the_recent_week_ac6: True` (mounted with `scrollLeft` at the right edge, ±1 px); screenshots `activity-1440.png` (1440×900), `activity-1080x1920.png`, `activity-390.png`, `activity-live-390.png` | pass |
| AC7 | Colours come only from existing tokens through `color-mix()`; `tests/test_ui_tokens.py` passes unchanged; the OpenAPI check passes | The heatmap section of `frontend/src/styles/app.css` uses `var(--sunk)`, `var(--line)`, `var(--accent)`, `var(--warning)`, `var(--fg-muted)` and `color-mix(in srgb, …)` only — no new colour literal; `activity_value_cell_carries_the_fill_share_ac7` proves the token resolves to the accent at 100 % and the sunk colour at the 20 % floor; `pytest -q tests/test_ui_tokens.py` → 21 passed (file untouched); full suite 374 tests, exit 0; `scripts/export_openapi.py --check` → exit 0, no diff | pass |
| AC8 | Spec D7, the scenarios and README describe the panel; a status doc records executed evidence only | `docs/specification.md` D7 (eight-panel layout, the Activity-grid bullet, the day-window cadence sentence in D7's polling paragraph), `docs/acceptance-scenarios.md` scenario 31 plus the new section *H. Year activity heatmap* with scenarios 47/48/49, `README.md` *Delivery planning* links this plan and this document, and this file, whose every number is quoted from the run in §4 | pass |

## 4. Executed commands

| Command | Result |
|---|---|
| `cd frontend && npm ci` | exit 0 |
| `cd frontend && npm run typecheck` (`vue-tsc --noEmit`) | exit 0 |
| `cd frontend && npm run build` (Vite → `src/jarvis_dashboard/static`) | exit 0 — `index-BjOyWiwV.css` 25.67 kB, `index-C7jmaASc.js` 270.45 kB, built in 0.5 s |
| `cd frontend && node probe_heatmap.ts` | `PROBE PASSED` — 94 checks |
| `cd frontend && node --import ./probe_register.mjs probe_cadence.ts` | `PROBE PASSED` — 11 checks |
| `PYTHONPATH=src .venv/bin/python scripts/smoke_frontend.py` | `FRONTEND SMOKE PASSED` — 277 `[ok]` lines, no `[FAIL]`, 371 browser requests, including `phase 6b: the year activity grid reads one day window and keeps its cadence` (37 activity checks) and `phase 6c: the year activity grid against a seeded year of volume` (41 activity checks); `activity_screenshots_written = ['activity-1080x1920.png', 'activity-1440.png', 'activity-390.png', 'activity-live-390.png']`. Run 15 (`/tmp/smoke15.log`) is the pass quoted in §3; run 17 (`/tmp/smoke17.log`) repeated it on the final tree — after the §6 fixes and the review fixes (the disclosure gated on the payload, the `window` shadow renamed, the `api_failure` activity checks added) — with the same receipts: `activity_worst_pct_deviation = 0.047162`, `activity_seeded_table_row_ac4_2026-10-15 = ['2026-10-15', 'no data', 'no data', 'no data', 'collection_gap · partial']` |
| `PYTHONPATH=src .venv/bin/python scripts/seed_history.py --db <tmp>/seeded/appdata/dashboard.sqlite3 --window-days 60 --cadence-recent 300 --case-days idle_day=5,collector_downtime=12 --manifest <tmp>/seeded-manifest.json` (the command `activity_seeded` runs) | exit 0, `seeder_seconds = 130.6`, 8 series, manifest window `2026-08-28T00:00:00Z … 2026-10-27T20:42:52Z` |
| `PYTHONPATH=src .venv/bin/python -m pytest -q` | exit 0 — 374 tests |
| `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_ui_tokens.py` | exit 0 — 21 tests |
| `PYTHONPATH=src .venv/bin/python scripts/export_openapi.py --check` | exit 0 |

`activity_seeded` seeds a temporary SQLite database, serves it with the real
`jarvis_dashboard.cli serve` against the loopback fixture, and compares the rendered grid
against `GET /api/v1/history?bucket=day` for the same series. Every per-day state, ratio,
pct, partial flag, tooltip and table row is therefore compared against an independent
consumer of the same contract, not against the component's own arithmetic. The window the
phase asserts on is read back from the request URL the page actually sent, so a UTC
midnight crossing during the phase cannot make the expectations drift.

## 5. Disposable probes (kept, not production)

- `frontend/probe_heatmap.ts` — runs the pure model under Node 24 (`node probe_heatmap.ts`).
  Chosen because the frontend has no JS test runner (`package.json` scripts are `build`,
  `typecheck`, `gen:types`, `verify`), and the window/scale arithmetic is worth pinning at
  unit speed: it covers five dates for the window, the Sunday alignment, the future-cell
  cap, all three states, the exact-string maximum above 2⁵³, the floor and top of the
  scale, partial flags, out-of-window buckets, duplicate labels, and the month labels
  against an independently derived column list.
- `frontend/probe_cadence.ts` with `frontend/probe_loader.mjs` / `frontend/probe_register.mjs`
  — the `activityDue` matrix; the loader resolves the repo's extensionless ESM imports for
  plain `node`.
- Both are self-contained (exit 1 on the first failed check) and live outside
  `frontend/src`, so they are not in the bundle. The same behaviour is asserted
  end-to-end by the smoke phases, which is why they stay disposable.
- Scenario 49's last-drawn-year clause is not probeable at unit speed (it is a store
  behaviour under a dead API), so it is covered in the browser by phase 7d
  (`api_failure_activity_kept`) with no added runtime: the existing phase already kills
  the dashboard process, and the heatmap's cells are compared before and after.

## 6. Defects found by the verification path (production)

1. **`scripts/seed_history.py` could not seed at all** — `KeyError: 'cached_input'` in
   `TrafficModel.advance` (`step = self.scales[index] * weight * COUNTER_RATIO[key] * …`):
   the cost-estimate work added a fifth counter (`radiance:linear_cached_tokens_total`)
   that the seeder's `COUNTER_RATIO` never learned. Fixed by `CACHED_INPUT: 0.35` in
   `COUNTER_RATIO` — kept well below `REQUEST_INPUT: 0.9` so the cost layer can never see
   a cache hit larger than the request it belongs to — and by passing the cached counter
   in **both** branches of `TrafficModel.publish`; the `request_absent` branch has to pass
   it too, because `install_fixture` replaces the whole value set of the series. This is
   outside the plan's stated scope, and the plan's Step 6 CLI cannot run without it.
   Verified: exit 0 (`seeder_exit: 0`, `seeder_seconds = 130.6`) and the per-day
   comparison of `phase 6c` against the API.
2. **`ActivityPanel.quality()` hid the reason a day is hollow** — it answered
   `unobserved` for every cell drawn as `none`, so a day the API reported with
   `processed_total = null`, `quality_flags = ["collection_gap"]` and `partial` rendered as
   plain `unobserved` in the tooltip and the table (AC3/AC4 require the quality). Caught
   by `activity_seeded_table_row_ac4_2026-10-15`, which printed
   `['2026-10-15', 'no data', 'no data', 'no data', 'unobserved']` against the API's
   `… · collection_gap · partial`. Fixed: the model carries `HeatmapCell.observed` (a
   bucket exists for that day) and `quality()` reports the API flags for a reported day
   with no total, reserving `unobserved` for days with no bucket at all. Two assertions in
   `frontend/probe_heatmap.ts` pin the distinction ("absent day is unobserved",
   "hollow day keeps its flags").
3. **The table invented data while the panel was still loading** (found by the bounded diff
   review, not by a check): the `activity-disclosure` block sat outside the payload branch,
   so during the skeleton it rendered 360 rows of `no data` — a claim of known absence
   before any read had completed. Fixed by moving the disclosure inside the payload branch;
   the focus order is unchanged (`chart-disclosure → activity-disclosure` still passes) and
   run 17 reproduced every seeded receipt. The same review renamed the local `window`
   binding in `refresh()` to `activityRange`: it shadowed the global `window`, which the
   file uses for `setInterval`/`addEventListener` outside that function, and the next
   reader would not have to discover that by accident.

Harness migrations (verification code, not production): the preflight mockup-machinery
guard now matches the mockup's root switcher (`html[data-state`, `data-state="live"`)
instead of any `data-state`, because AC2 mandates `data-state` on heatmap cells;
`ranges()` excludes the fixed day-window URL so it keeps asserting on the range the user
selected; `poll_lifecycle` samples its fetch baseline after the tab is hidden, since a
refresh started just before the hide has already recorded its URL; the keyboard walk
asserts the contract's order rather than an exact index count, because the panel adds a
focusable disclosure and, with two series in the picker, the picker itself.

## 7. Working-tree interference with the green run

`dom_utc_labelled` (an existing gate in `scripts/smoke_frontend.py`) asserts that each
summary card's text contains `UTC`. The checkout carries an **uncommitted change that is
not part of this task** — `frontend/src/components/Card.vue` minus
`<span class="utc">UTC</span>` (the card header marker; `git diff` shows it as the only
change to that file, alongside the uncommitted `frontend/src/lib/format.ts` share feature
this task also did not author). With that tree, the suite aborts in phase 4:

```
[FAIL] dom_utc_labelled: False (expected True)
[receipt] dom_cards_text = ['card-today | Observed today960Computed input900 · 94%…', …]
```

This is a pre-existing failure of an existing gate, so it was not "fixed" by editing the
unrelated component. To obtain a full transcript for the surface this task owns, the run
of §4 was taken with the marker restored (that bundle is `index-PvPDCDxB.js`); `Card.vue`
was then put back byte-exact (`diff` against the backup is empty) and the tree rebuilt, so
the delivered bundle is `index-BdPKJ8wR.js` — the build of the tree as it stands. Every
activity check in §3 is independent of that card; the `dom_cards_text` receipt above is the
record of the difference, and the delivered tree needs the card's `UTC` marker restored (or
the gate updated) before `scripts/smoke_frontend.py` can pass end to end.

## 8. NOT EXECUTED

- **D9 resource workload (scenario 33)** — out of the plan's scope and unchanged by it.
  The panel adds at most one extra reader per 300 s inside the existing sequential
  refresh, and spec D9's 366-day p95 has never been measured on this host. The gate stands
  as an operator/qualification task and must now include this read.
- **Real Radiance engine** — every number here comes from the loopback fixture and the
  local seeder. The panel reads only the documented `/api/v1/history` contract
  (`export_openapi.py --check` passes), so no provider/integration evidence is claimed.
- **Container image gates** — `docker` is absent on this host; the built bundle was
  exercised through `jarvis_dashboard.cli serve` serving `src/jarvis_dashboard/static`.
- **A full 367-day seed** — `activity_seeded` uses the plan's Step 6 CLI with
  `--window-days 60`, so the window holds ~60 observed days plus hollow days. A full-year
  seed was not run; the window arithmetic (358 + `getUTCDay`, 52 columns, the 366-bucket
  cap) is pinned by `probe_heatmap.ts` for five dates including a leap day.
- **Headful visual review** — screenshots were taken headless at 1440×900, 1080×1920 and
  390×844. The low end of the fill ramp on `--sunk` is deliberately below 3:1 non-text
  contrast (as on GitHub); the tooltip, table and `aria-label` carry those values instead.
