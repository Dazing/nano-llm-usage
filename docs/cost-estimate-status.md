# API-equivalent cost estimate — implementation evidence

Receipts: [`docs/receipts/cost-estimate/`](receipts/cost-estimate/). Plan:
[cost-estimate-implementation-plan.md](cost-estimate-implementation-plan.md).
Date: 2026-10-27 (UTC). Contract: specification
[D11](specification.md#d11-api-equivalent-cost-estimate), scenarios
[41–46](acceptance-scenarios.md#g-api-equivalent-cost-estimate-specification-d11).

## 1. Summary

- **Outcome:** the estimate is implemented end to end — a fifth counter
  `cached_input` (`radiance:linear_cached_tokens_total`) that is parsed, baselined,
  gap-tracked and stored like `request_input`; schema version 3; three startup-only
  rate variables; a read-time `api_equivalent` object (amount, token split, cache
  share, priced/expected hours, `cache_exceeds_input`); `cached_input` counter and
  gap scopes on every range; a `cost_rates` block on `/status`; and the dashboard
  money line, cache-share line, coverage note, trend line and diagnostics scope.
- **Not money:** nothing monetary is written to SQLite. The durable smoke asserts
  `PRAGMA table_info(samples)` contains no cost/amount column, and a rates-free
  restart on the same database reprices the identical history.
- **Not claimed:** the container image gates (no `docker` on this host, §4) and the
  real-engine operator gate (§7).
- **Defect the browser phase caught:** `formatMoney` treated the schema's
  6-decimal amount string as mills instead of micro-dollars, so a `2.880000`
  range rendered `≈ $28.80`. Fixed in `frontend/src/lib/format.ts`
  (`(micros + 5_000n) / 10_000n`); the browser phase now asserts the rendered
  money line equals the API's own amount for the same range.

## 2. Environment (executed gates)

| Field | Value |
|---|---|
| OS / kernel | `Linux 7.2.11-1-cachyos (x86_64)` |
| Python | 3.13.17 (`.venv`); host `python3` is 3.11.2, the project requires ≥3.11 |
| Node / npm | v24.22.0 / 11.19.1 |
| Browser | Chromium, driven over CDP by `scripts/browser_driver.py` |
| Docker / compose | absent — `docker: command not found`; image gates NOT EXECUTED (§4) |
| Git | HEAD `301a118` on `main`; 32 tracked files changed (+1776/−67) plus 6 new paths: migration 0003, `tests/test_cached_input_migration.py`, this status document, the plan, `docs/receipts/cost-estimate/` |
| Upstream | `tests/fixtures/fixture_server.py` (loopback fixture). The real Radiance engine was **not** contacted. |

`PYTHONPATH=src` is required for `python -m jarvis_dashboard…` on this host (the
editable-install `.pth` in `.venv` points at a stale path); `pytest` and the
`scripts/*` harnesses set it themselves.

## 3. Acceptance criteria (plan AC1–AC8)

| AC | Requirement | Evidence | Result |
|---|---|---|---|
| AC1 | 1,000,000 in / 600,000 cached / 100,000 out at 3 / 0.30 / 15 → `2.880000`, ratio 0.6, `≈ $2.88`, `Cache hits 60% of input` | `tests/test_queries.py::test_a_fully_observed_hour_prices_to_the_reference_amount`; `scripts/smoke.py` scenario 9 over HTTP (`amount 5.760000`, `cache hit ratio 0.6`, `priced / expected hours = 1 / 1`); `scripts/smoke_frontend.py` `cost_money_line_ac1` + `cost_share_line_ac1` (the rendered card equals the API's `api_equivalent` for the same range) | pass |
| AC2 | A month whose cache counter starts on day 14 prices only the tracked hours and reports coverage | `tests/test_queries.py::test_only_hours_with_cache_tracking_are_priced` (priced 1 / expected 3, `partial`), `test_expected_hours_count_complete_hours_only`; `scripts/smoke_frontend.py` coverage-note shape (`partial · covers … days`) | pass |
| AC3 | A gap hour that leaves any priced counter without an observed interval is unpriced and lowers coverage; primary gap reporting unchanged | `tests/test_queries.py::test_an_unobserved_cached_hour_leaves_the_hour_unpriced` (priced 1 / expected 2, `gap_seconds["cached_input"] > 0`); the pre-existing gap suite (`tests/test_gaps.py`, `test_queries.py`) passes unchanged | pass |
| AC4 | No rates → `amount` null, share and coverage keep answering; 1–2 of 3 set → startup `ConfigError` naming the missing variables | `tests/test_config.py::test_partial_cost_rates_name_every_missing_variable` (+ 6 other rate tests); `scripts/smoke.py` scenario 9 rates-free restart (`amount is null without rates`, `cache share survives`, `status reports no prices when unset`); `scripts/smoke_frontend.py` `rates_free_hides_money_ac4`, `rates_free_hides_footnote_ac4`, `rates_free_keeps_share_ac4`, `rates_free_keeps_coverage_ac4` | pass |
| AC5 | Cached ahead of request over the priced hours → uncached priced as 0 with `cache_exceeds_input`, never silently clamped | `tests/test_queries.py::test_cached_input_ahead_of_request_input_is_flagged_not_clamped` (`uncached_input "0"`, `flags ["cache_exceeds_input"]`, `amount 1.860000`, ratio 1.2); the UI flag line is wired in `SummaryCards.vue` (`CACHE_SKEW_NOTE`) | pass |
| AC6 | A rate change plus restart reprices history; no money in SQLite | `tests/test_queries.py::test_changing_rates_reprices_the_same_stored_history` (`2.880000` → `0.960000`, no cost/amount table); `scripts/smoke.py` scenario 9 (`stored money columns = []`) | pass |
| Money integrity | An unchanged or flapping scrape must never add money | `tests/test_queries.py::test_repeated_identical_polls_never_recredit_the_estimate` (10 identical republishes → `2.880000`, priced 1); `docs/receipts/cost-estimate/keeper_republish_probe.py` and `flap_republish_probe.py` (10 drop/return cycles then 10 long drops → amount and tokens unchanged) | pass |
| AC7 | v2 → v3 with a verified `pre-migration-3` snapshot; A2 totals intact; old samples keep `cached_input = null` | `tests/test_cached_input_migration.py` (4 tests: snapshot verified at schema 2 + totals `(computed_input 900, generated_output 60)` preserved; pre-cache samples all `(None, None)` and no cached hour row; idempotent reopen; post-migration polls write raw + delta + hour) | pass |
| AC8 | A source with no cache counter: the other four counters unaffected, nothing priced, the cached-input scope visible as its own diagnostics gap | `tests/test_queries.py::test_a_source_without_cache_tracking_prices_nothing`; `scripts/smoke.py` scenario 9 (engine-only `processed_total 0`); `scripts/smoke_frontend.py` `cost_unpriced_is_no_data_ac8`, `cost_unpriced_share_ac8`, `cost_unpriced_coverage_ac8` (`partial · covers 0…`), `cost_diagnostics_names_cached_input` | pass |

## 4. NOT EXECUTED — container image gates

`docker` is absent on this host, so `scripts/qualify_image.py` (build, non-root,
512 MiB, volume survival) and `scripts/qualify_resources.py` were not run — the
same limitation recorded in [`live-panel-status.md`](live-panel-status.md) §4.
Executed as far as the host allows:

- `scripts/qualify_image.py --docs-check` → `checks=56 failures=0` (the compose
  passthrough rows for the three rate variables are part of that set).
- `.venv/bin/python -m pytest -q` → **376 passed** (host, no container).
- `.venv/bin/python scripts/smoke.py` → **SMOKE PASS** (scenarios 1–9, real
  `collect`/`serve` subprocesses against the loopback fixture, schema 3).
- `.venv/bin/python scripts/smoke_frontend.py` → **FRONTEND SMOKE PASSED**
  (§5; 1440 px and 390 px shots in [`receipts/cost-estimate/`](receipts/cost-estimate/)).

Operator checklist for the image gate: build the image, set the three
`COST_RATE_*` variables in `.env`, recreate the container, and follow
[`deployment.md` → Cost rates](deployment.md#cost-rates-the-api-equivalent-estimate).

## 5. Commands executed

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | 376 passed, exit 0 (the project's `-q` config prints no summary line; the exit status is the signal) |
| `.venv/bin/python -m pytest -q tests/test_metrics_parser.py tests/test_accounting.py` | 40 passed |
| `.venv/bin/python -m pytest -q tests/test_cached_input_migration.py tests/test_metadata_migration.py tests/test_storage.py tests/test_backup.py` | passed |
| `.venv/bin/python -m pytest -q tests/test_config.py` | 44 passed |
| `.venv/bin/python -m pytest -q tests/test_queries.py tests/test_api.py` | passed (9 new cost query tests, 5 new API tests) |
| `.venv/bin/python scripts/export_openapi.py --check` | exit 0 (snapshot matches the app) |
| `cd frontend && npm run verify` | `gen:types` + `vue-tsc --noEmit` + `vite build` → `static/assets/index-D71cXh7U.js` (256.04 kB) |
| `.venv/bin/python scripts/smoke.py` | SMOKE PASS (scenario 9 receipts quoted in §3) |
| `.venv/bin/python scripts/smoke_frontend.py` | FRONTEND SMOKE PASSED, 0 `[FAIL]` lines, full transcript in [`receipts/cost-estimate/frontend-smoke.txt`](receipts/cost-estimate/frontend-smoke.txt): API `api_equivalent` = `2.880000` / tokens `400000,600000,100000` / ratio `0.6` / priced `1` of `1`, card `≈ $2.88` + `60% of input`, footnote verbatim, coverage note `partial · covers 0.0 of 26.5 days`, trend line `API-equivalent ≈ $2.88 · Cache hits 60% of input · partial · covers 1 of 24 h`, mobile card `≈ $2.88`, unpriced series `no data` + `Cached input` diagnostics line, rates-free restart hides the money line and footnote while the share and coverage note stay |
| `docs/receipts/cost-estimate/keeper_republish_probe.py`, `flap_republish_probe.py` | disposable in-process probes: identical republishes, then 10 drop/return cycles and 10 long drops, leave the amount at `2.880000` with tokens unchanged |
| `.venv/bin/python scripts/qualify_image.py --docs-check` | `checks=56 failures=0` |

## 6. Deviations from the plan

- **Browser money value asserted against the API, not a constant.** The plan's
  Step 7 asked the browser suite to assert a priced `≈ $…` value. The phase first
  asserted a fixed `≈ $2.88`, which exposed the `formatMoney` unit bug (§1): the
  DOM read `≈ $28.80` for an API `2.880000`. The check now derives its expectation
  from the API's own `api_equivalent` for the same range and series
  (`money_text`/`share_text` mirror the bundle's formatters), so the card, the
  trend line and the mobile card must all agree with the served amount, and the
  shape checks (`≈ $1,234.56` / `< $0.01` / `no data`) stay as the guard against a
  raw 6-dp string reaching the DOM. The suite's keeper republishes the fixture
  every 20 s, so the priced volume in the current hour is not a suite constant —
  `test_repeated_identical_polls_never_recredit_the_estimate` proves the volume
  itself cannot grow from an unchanged scrape.
- **A brand-new series has no coverage note.** The browser AC8 phase accepts an
  empty note as well as `partial · covers 0…`: a series created seconds ago has no
  complete expected hour, and plan D4 does not make `partial` true in that case.
- **`optional_ok` unchanged.** `src/jarvis_dashboard/collector.py` keeps its
  availability probe on the two request counters, per plan D1: a source without
  cache tracking must not read as a lost optional capability.
- **Priced hour counts rows, not spans.** Plan D4 says an hour "with a collection
  gap" is unpriced. `hourly_totals` records observed intervals, so the rule is
  implemented exactly as the plan's own precision note requires: all three priced
  counters need `observed_intervals > 0` and a non-null token total in that hour. A
  counter absent for a whole hour produces no row at all, which is why gap hours are
  unpriced (AC3 test).
- **History range costs are priced to `min(to, now)`.** Future `to` bounds must not
  invent expected hours; `read_history` clamps the coverage window the same way the
  range payload clamps `ended_at`.

## 7. Operator gates that remain

1. **Real engine cache counter.** Confirm `radiance:linear_cached_tokens_total`
   exists on the real Radiance build, is monotonic, and counts the same request-scoped
   prompt tokens the engine's linear cache serves. If it is absent, the estimate
   stays unpriced by design; the dashboard's `Cached input` diagnostics line is the
   observable. Never read a `no data` estimate as free traffic.
2. **Price choice.** The three variables are a personal API-equivalent tariff, not a
   Jarvis bill. Re-check them against the provider's current published prices; a rate
   change reprices all history on the next `docker compose up -d`.
3. **Image gates** (§4) on a host with a container runtime, including the 512 MiB
   memory gate with the new counter in the scrape payload.
4. **Backup discipline across the migration.** The first start on an existing volume
   writes `<database>.pre-migration-3.sqlite3` next to the database; confirm the
   volume's normal backup routine captures it before the first prune.
