# Delivery 3 status — evidence from the machine that ran it

Executed on 2026-10-26 (UTC) on the development host, not inside the shipped container.

## Environment

| Item | Value |
| --- | --- |
| OS | Linux 7.2.11-1-cachyos (x86-64) |
| Node.js / npm | v24.22.0 / 11.19.1 (build-time only; the image pins `node:24.22.1`) |
| Browser | Chromium 156.0.8078.47 (Debian 12 build), headless, driven over CDP by `scripts/browser_driver.py` |
| Python (project venv) | 3.13.17 — `fastapi` 0.162.0, `starlette` 1.10.0, `uvicorn` 0.54.0 |
| Repository state | git `249a741` plus the uncommitted Delivery 2 and Delivery 3 working tree |
| Container runtime | none installed (no `docker`, no `podman`) |

Commands were run from the repository root with `.venv/bin/python` and `npm` on `PATH`.

## Commands executed

| Command | Result |
| --- | --- |
| `.venv/bin/pytest -q` | exit 0 — 244 passed, 0 failed |
| `.venv/bin/pytest tests/test_static.py tests/test_ui_tokens.py tests/test_config.py -q` | exit 0 — 62 passed |
| `cd frontend && npm ci` | exit 0 — 79 packages from the committed `package-lock.json` |
| `cd frontend && npm run verify` (gen:types → `vue-tsc --noEmit` → `vite build`) | exit 0 — 42 modules, built in ~0.3 s |
| `.venv/bin/python scripts/export_openapi.py --check` | exit 0 — 7 paths, 22 schemas, no `422`/`ValidationError` in the snapshot |
| `.venv/bin/python scripts/browser_driver.py --self-test` | exit 0 — `selftest ok`, Chromium launched, one page evaluated, screenshot written |
| `cd frontend && DASHBOARD_STATIC_OUT=/tmp/fe-static npm run build && ls /tmp/fe-static/index.html /tmp/fe-static/assets` | exit 0 — the Dockerfile's `DASHBOARD_STATIC_OUT` path works outside the image |
| `.venv/bin/python scripts/smoke_frontend.py` | exit 0 — 65 checks, `FRONTEND SMOKE PASSED` |
| `.venv/bin/python scripts/smoke.py` | scenarios 1–7 pass; scenario 8 fails at `restore refuses a running target: 0 != 3` (pre-existing Delivery 1/2 test-orchestration race, see "Adjacent defect") |

Bundle shape from the final `npm run build` into `src/jarvis_dashboard/static/`: `index.html` 0.59 kB,
`assets/index-*.css` 6.75 kB (1.85 kB gzip), `assets/index-*.js` 250.28 kB (89.65 kB gzip),
`grep -c "https://" index.html` = 0.
Chart.js tree-shaking: 0 hits for `LineController`, `PieController`, `DoughnutController`,
`PolarAreaController`, `BubbleController`, `ScatterController`, `TimeScale`, `LogarithmicScale`,
`TimeCategoryScale`, `RadialLinearScale` in the bundle (bar + category/linear + legend/tooltip only).

## Front-end smoke receipts (`docs/receipts/delivery-3/smoke-frontend.log`)

```
[ok ] openapi snapshot current: 0            [ok ] range_30d_bucket: 'day'
[ok ] serve ready on port 54097              [ok ] custom_bucket: 'day'
[ok ] api_processed_total: '960'             [ok ] custom_over_limit_blocked: 5
[ok ] api_lifetime_input: '900'              [ok ] chart_table_rows_rendered: True
[ok ] api_lifetime_output: '60'              [ok ] null_bucket_label: True
[receipt] root_response = 200 text/html      [ok ] null_bucket_never_zero: False
[ok ] dom_lifetime_total: '960'              [receipt] null_bucket_row = no data | no data | no data | complete
[ok ] dom_lifetime_input: '900'              [ok ] canvas_count: 1
[ok ] dom_lifetime_output: '60'              [ok ] incomplete_note: True
[ok ] range_24h_bucket: 'hour'               [ok ] state_empty: True
[ok ] state_baseline: True                   [ok ] state_optional_unavailable: 2
[ok ] offline_banner: True                   [ok ] state_offline_history_kept: '974'
[ok ] offline_recovers: True                 [ok ] state_api_failure_stale: True
[ok ] api_failure_history_kept: '974'        [ok ] api_failure_recovers: True
[ok ] poll_timers: 1                         [ok ] poll_interval_ms: 15000
[ok ] polls_paused_when_hidden: 66           [ok ] poll_resumes: True
[ok ] theme_persisted: 'light'               [ok ] theme_after_reload: 'light'
[ok ] reduced_motion: '0s'                   [ok ] keyboard_focus_visible: 6x '2px'
[receipt] keyboard_focus_sequence = ['theme-toggle', 'range-24h', 'range-7d',
           'range-30d', 'range-custom', 'chart-disclosure']
[ok ] picker_lists_second_series: True       [ok ] race_no_stale_overwrite: True
[ok ] browser_requests_local_only: []        [ok ] probe_fetches_local_only: []
[ok ] no_fixture_traffic: []                 [receipt] console_errors = 0
[receipt] fixture_requests_delta = 2         [receipt] inline_data_urls = 2
[receipt] screenshots_written = 9            [receipt] browser_requests = 115
FRONTEND SMOKE PASSED
```

`polls_paused_when_hidden` is the fetch count sampled over a 12 s hidden window: the value is
identical before and after, so no request left the page while hidden. `fixture_requests_delta`
is collector polls over the same window (2 polls at `POLL_INTERVAL_SECONDS=5`), with
`fixture_sees_no_browser_traffic: []` proving no browser traffic reached the fixture origin.
`inline_data_urls` counts Chromium's own `<input type="date">` calendar glyphs (`data:` URLs,
not network requests); every real request was same-origin `http://127.0.0.1:<app port>/`.

## Screenshots

`docs/receipts/delivery-3/` — desktop-1440-normal.png, desktop-1440-dark.png,
desktop-1440-custom-range.png, desktop-1440-chart-table.png, desktop-1440-diagnostics.png,
desktop-1440-offline.png, mobile-390-empty.png, mobile-390-normal.png, mobile-390-stale.png.
They are visual evidence only: legibility, colour-blind separability, screen-reader phrasing,
touch-target comfort and overall taste need a human pass (below).

## Cache and snapshot contract (AC10/AC11)

| Path | Status | `cache-control` | Body |
| --- | --- | --- | --- |
| `GET /` (index.html) | 200 | `no-cache` | built HTML |
| `GET /assets/<hashed>` | 200 | `public, max-age=31536000, immutable` | built asset |
| `GET /api/v1/status` | 200 | `no-store` | JSON, `schema_version: 1` |
| `GET /api/v1/summary` | 200 | `no-store` | JSON |
| `GET /api/v1/openapi.json` | 200 | `no-store` | frozen snapshot |
| `GET /health/live`, `/health/ready` | 200 | `no-store` | JSON |
| `GET /api/v1/definitely-missing` | 404 | `no-store` | `{"error":{"code":"not_found"}}` |
| `GET /missing.txt` | 404 | `no-store` | `{"error":{"code":"not_found"}}` |
| `POST /health/live` | 405 | `no-store` | `{"error":{"code":"method_not_allowed"}}` |
| generic 500 through `NoStoreMiddleware` | 500 | `no-store` | payload-free error |
| `GET /` with no built `index.html` | 404 | `no-store` | `{"error":{"code":"not_found"}}` |

Proven by `tests/test_static.py` (11 tests) against the same ASGI graph `uvicorn` serves
(`create_asgi_app` → `NoStoreMiddleware` → page layer → FastAPI app), plus
`tests/test_ui_tokens.py` (token completeness + WCAG contrast: ≥4.5:1 for text/muted/error/warning
on both panels, ≥3:1 for the two chart series, `--series-input` ≠ `--series-output` in both themes)
and `tests/test_config.py` (`STATIC_DIR` unset/empty → packaged `src/jarvis_dashboard/static`,
whitespace-only → `ConfigError`, value stripped).

## Scenarios covered

`A2` accounting (1000/100 → 1600/140 → 1900/160 with two polls between: 900 in, 60 out, 960 total,
identical in the API payload and in the DOM), `C19`/`C20` presentation of nulls, gaps, partial and
incomplete buckets, `E31` stale/offline/empty/baseline/optional-unavailable states, `E32`
same-origin serving with no CDN, no web fonts and no fixture traffic. Delivery 3 acceptance criteria
AC1–AC9 are covered by `scripts/smoke_frontend.py`; AC10 is covered by the table above; AC11 is
covered on the host side (bundle grep for CDN hosts, `--check` gate, `npm ci` from the lockfile) —
its image half is not executed here.

## Not executed on this machine

- `docker build` and `docker compose up` — no container runtime installed. The image's Node stage
  is verified only by reading (Node/npm lines appear above `AS runtime` only) and by running the
  same build locally with `DASHBOARD_STATIC_OUT=/tmp/fe-static`.
- Running the container under its declared limits (read-only rootfs, 256 MiB, 0.5 CPU, uid 10001)
  and any restart/recreate behaviour.
- D9 resource work (steady-state RSS/CPU under load, GC/heap behaviour) — that is Delivery 4.
- Live `Jarvis` / Unraid checks: the operator gate (base URL, API key, source id, port, bind
  address, retention) stays with the deployment operator.
- Cross-browser: only Chromium 156 was driven. Firefox/Safari are unexercised.
- Human judgement: visual taste, colour-blind separability, screen-reader output, touch comfort.
- Memory-leak measurement over long sessions: the smoke proves one timer, paused when hidden,
  and a bounded data set; it does not measure heap growth.

## Adjacent defect found, not fixed (out of Delivery 3 scope)

`scripts/smoke.py` scenario 8 asserts `restore refuses a running target` → `0 != 3`, reproduced in
three runs. The product contract is intact: a restore into a target whose volume lock is provably
held exits 3 with `restore refused: volume_locked` (probe `/tmp/probe_restore_lock.py`, lock probe
reads `fcntl.flock` on `<db>.lock`). The script starts the collector and runs the busy restore
~0.45 s later, using `has_series(target)` as readiness — which is already true from the restored
snapshot — so the restore wins the race before `storage.acquire_volume_lock` (`storage.py:82-95`)
takes the lock. Suggested fix for the Delivery 1/2 owner: wait for the lock file to be held (or for
the collector's readiness line) before asserting the busy-restore exit code.

## Design decisions this delivery deviated on (with cause)

- Starlette 1.10 `StaticFiles` has no `headers` argument, and mounting a sub-app inside FastAPI
  breaks the API's 405/404 contracts and the frozen OpenAPI snapshot, while
  `BaseHTTPMiddleware` + `FileResponse` raises `RuntimeError: Unexpected message received:
  http.request`. The page layer is therefore a pure-ASGI app outside the FastAPI app
  (`app.py` `install_static`/`_PageLayer`) with `_HeaderStamper` applying `no-store` to
  `/api*`, `/api/v1*`, `/health*` only.
- The page fires one in-flight read set (`/api/v1/summary` before `/api/v1/history`), because the
  server admits 8 concurrent readers and a 6-request load burst returned 503
  `read_capacity_exceeded` — a single refresh now occupies at most two slots.
- `baseline-note` fires when a series has zero accepted intervals (`lifetime.processed_total` is
  null and there is no gap flag), not on `first_observation_at`: a series with accepted intervals
  and a real gap must show the gap, not a baseline note.
- `scripts/browser_driver.py` sends a CDP websocket ping every 5 s; headless Chromium stops
  servicing websocket reads when the page sits idle, and the smoke's long fixture waits
  (up to 75 s) stalled `Runtime.evaluate` without it.
- Front-end dependency pins are exact (`typescript` 5.9.3, not 7.0.2, because
  `openapi-typescript` 7.13.0 requires `^5`) and `package-lock.json` is committed.
- `tests/asgi_client.py` (Delivery 2 harness) had to change: its `receive()` returned
  `http.request` forever, so Starlette's `FileResponse` — which polls for a disconnect under the
  ASGI 2.3 spec uvicorn advertises — spun instead of finishing. It now delivers the body once and
  then awaits cancellation, matching a live connection. No production behaviour changed.
