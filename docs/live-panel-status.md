# Live usage panel status — implementation evidence

Receipts: [`docs/receipts/live-panel/`](receipts/live-panel/). Plan:
[live-panel-implementation-plan.md](live-panel-implementation-plan.md).
Date: 2026-10-27 (UTC). Contract: specification
[D10](specification.md#d10-live-usage-panel), scenarios
[35–40](acceptance-scenarios.md#f-live-usage-panel-specification-d10).

## 1. Summary

- **Outcome:** the live panel is implemented end to end — a 1 Hz live poller thread
  (`src/jarvis_dashboard/live.py`) owned by `serve`, a snapshot-only
  `GET /api/v1/live`, the Vue `LivePanel` row with five tiles and a Live/Stale/Starting
  chip, fixture support for the live fields plus a representative-payload mode, the
  browser smoke's live phase, and the 512 MiB limit raised through Compose and the
  qualification harness. The full host-side gate set passes.
- **Not claimed:** the resource gate (plan AC10 / specification D9 workload). This host
  has no container runtime (`docker` is absent), so `scripts/qualify_resources.py` and
  every image gate are recorded NOT EXECUTED with prerequisites (§4).
- **Isolation evidence:** `src/jarvis_dashboard/live.py` imports no storage module
  (`grep -c "sqlite\|storage"` → 0), the durable smoke's totals are unchanged
  (900 / 60 / 960), and the durable collector's `/metrics` traffic is unchanged
  (2 collector hits per 10 s window beside the poller's 10).

## 2. Environment (executed gates)

| Field | Value |
|---|---|
| OS / kernel | `Linux 7.2.11-1-cachyos (x86_64)` |
| CPU / memory | AMD Ryzen 7 1800X (16 cores), 32,776,308 kB (same host as [`delivery-4 preflight`](receipts/delivery-4/20261026T100600Z-preflight/environment.json)) |
| Python | 3.13.17 (`.venv`) |
| Node / npm | v24.22.0 / 11.19.1 |
| Browser | Chromium 156.0.8078.47 (Debian 12 bookworm), driven over CDP by `scripts/browser_driver.py` |
| Docker / compose | absent — `docker: command not found` |
| Git | HEAD `c5139b6` on `main`; 39 modified/untracked paths in the final tree (30 modified tracked files, +1156/−51, plus 9 new paths) |
| Upstream | `tests/fixtures/fixture_server.py` (loopback fixture). The real Radiance engine was **not** contacted. |

`PYTHONPATH=src` is required for every command on this host: the editable install's `.pth`
inside `.venv` points at a stale path, so `python -m jarvis_dashboard…` fails without it.

## 3. Acceptance criteria (plan AC1–AC10)

| AC | Result | Evidence |
|---|---|---|
| AC1 — current values within 2 s; `-` per-request at `running=0` | **EXECUTED** | [`smoke-frontend-transcript.txt`](receipts/live-panel/smoke-frontend-transcript.txt) live phase: `live-requests` `3 / 1`, `live-prefill` `1,240 tok/s`, `live-decode` `180 tok/s · 60/req`, then `180 tok/s · -` at `running=0`. Screenshots [`live-1440-ac1.png`](receipts/live-panel/live-1440-ac1.png), [`live-390-mobile.png`](receipts/live-panel/live-390-mobile.png). |
| AC2 — MTP windows: 5 s fills ≥5 s, 1 m ≥60 s, 30 m ≥1800 s; lifetime from the first sample | **EXECUTED** | Browser: 5 s slot at 6.1 s, 1 m slot at 61.2 s (`live_5s_window_seconds`, `live_1m_window_seconds` in the transcript). 30 m boundary and lifetime by injected clock: `tests/test_live.py::test_mtp_windows_fill_only_at_a_full_span_of_continuous_samples`, `::test_lifetime_uses_the_latest_cumulative_totals`. No 30-minute browser wait (plan §5). |
| AC3 — MoE like AC2 over `routed_res / routed`, no lifetime; no `experts` block ⇒ all `-` | **EXECUTED** | Browser: MoE `93% / - / -` at the 5 s mark, `93% / 93% / -` at ~61 s, and `- / - / -` with `FixtureState.suppress_experts = True` while MTP kept `70%`. Unit: `::test_moe_windows_fill_over_the_experts_pair_and_have_no_lifetime`, `::test_missing_experts_block_nullifies_only_the_moe_pair`. |
| AC4 — idle denominator ⇒ `-`, never `0%` | **EXECUTED** | Unit: `::test_an_idle_window_is_a_placeholder_never_zero_percent`, `::test_an_idle_moe_denominator_is_a_placeholder_too`. Browser: freezing the fixture clock (uptime ticker stopped, experts frozen) drove MoE to `- / - / -` while MTP kept its value ([`live-1440-run-reset.png`](receipts/live-panel/live-1440-run-reset.png)). |
| AC5 — restart / counter decrease / >3 s gap clear the run; windows refill from `-` | **EXECUTED** | Browser: fixture uptime dropped 400 s → MoE to `-`, then refilled to `93%` (the experts baseline lives in `/stats`). Unit: `::test_uptime_decrease_clears_the_stats_run`, `::test_a_counter_decrease_clears_the_metrics_run`, `::test_a_live_counter_decrease_clears_the_stats_run`, `::test_a_gap_of_more_than_three_seconds_clears_the_run`, `::test_a_three_second_gap_is_tolerated_by_the_run_rule`. |
| AC6 — 3 s staleness ⇒ `-`; both stale ⇒ row stale; durable collection, `/health/ready`, `/api/v1/status` unaffected | **EXECUTED** | Browser: `/stats` 500 + `/metrics` 500 → Stale chip in 4.0 s with every tile `-` (`live_outage_seconds` 73.6, [`live-1440-stale.png`](receipts/live-panel/live-1440-stale.png)), recovery → Live. `tests/test_service.py::test_live_failures_never_gate_readiness` (`/health/ready` 200 `{"status":"ready"}` while the fixture `/stats` returns 500). `tests/test_api.py::test_live_answers_while_the_read_admission_gate_is_saturated`. Durable half: [`smoke-transcript.txt`](receipts/live-panel/smoke-transcript.txt) `SMOKE PASS`. |
| AC7 — no live value in SQLite; durable volume unchanged | **EXECUTED** | `live.py` imports no storage/SQLite module; `scripts/smoke.py` totals unchanged (input 900, output 60, total 960) with `SMOKE PASS`; [`live-cadence.txt`](receipts/live-panel/live-cadence.txt) shows the collector's 2 hits per 10 s window unchanged next to the poller's 10. |
| AC8 — one 1 s + one 15 s timer per tab, both paused when hidden; browser never contacts Radiance | **EXECUTED** | `poll_lifecycle` receipts: `poll_timers [1000, 15000]`, `polls_paused_when_hidden` equal before/after the hidden window, `poll_timers_cleared_when_hidden 0`, resume on focus. `network`: `fixture_paths ['/metrics', '/stats']` only, `browser_requests 317` with none to the fixture origin. |
| AC9 — `mem_limit: 512m`, harness expects 536870912, docs state 512 MiB | **EXECUTED** | `compose.yaml` `mem_limit: 512m`; `scripts/qualify_common.py` `MEMORY_MAX = "536870912"`, `HostConfig.Memory` 536_870_912, compose check `"512m"`; `scripts/qualify_resources.py` `MEMORY_MAX_BYTES = 536870912`, `NON_RECLAIMABLE_BAR = 384 * MIB`. `scripts/qualify_image.py --docs-check` → `checks=50 failures=0` ([receipt](receipts/delivery-4/20261027T091437Z/)). Text search for `256 MiB`/`256m` outside `docs/delivery-*` and `docs/receipts/` finds nothing. |
| AC10 — D9 workload under 512 MiB with the live poller | **NOT EXECUTED** | §4. |

## 4. NOT EXECUTED — resource gate (AC10)

`scripts/qualify_resources.py` (and `scripts/qualify_image.py --build/--inspect/--cases`)
need a container runtime. This host has none, so the extended D9 workload — live poller at
1 Hz, representative `/stats` (55,583 B) and `/metrics` (3,872 B) payloads, one history plus
one live timer per tab, `memory.max = 536870912`, `cpu.max = 50000 100000` — has **no
measured peak cgroup memory, steady RSS or CPU number**. The harness is wired and its
configuration contract passes; the measurement is not.

Prerequisites on a qualifying host: Docker Engine + Compose v2, cgroup v2 with the `memory`
controller, root for the appdata `chown` and the ENOSPC tmpfs bind (delivery 4 §4), Chromium
(present here), and roughly 40 minutes of wall clock (300 s warm-up, 30 min soak, 30 s
history and 60 s tab sampling). Command:
`PYTHONPATH=src .venv/bin/python scripts/qualify_resources.py`.

Consequence for the CPU risk named in the plan (§6): the 1 Hz `/stats` parse plus the
`/metrics` line scan is new steady work at 0.5 CPU that has **not** been measured. Do not
read §3 as a CPU verdict.

## 5. Commands executed

| Command | Exit | Result |
|---|---|---|
| `PYTHONPATH=src .venv/bin/python -m pytest -q` | 0 | 330 tests, 0 failures, 0 errors, 0 skipped (junit XML; this pytest build prints no summary line, so counts come from `--junitxml`) |
| `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_live.py` | 0 | 73 tests (42 functions + parametrised cases) |
| `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_service.py tests/test_live.py` | 0 | 88 tests, 0 failures |
| `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_api.py` | 0 | 33 tests, 0 failures (4 live cases) |
| `PYTHONPATH=src .venv/bin/python scripts/export_openapi.py --check` | 0 | `frontend/src/api/openapi.json` in sync (8 paths, 26 schemas, `LiveResponse` present) |
| `cd frontend && npm run verify` | 0 | `gen:types` rewrote `src/api/schema.ts`; `vue-tsc --noEmit` clean; `vite build` wrote `src/jarvis_dashboard/static/` |
| `PYTHONPATH=src .venv/bin/python scripts/smoke.py` | 0 | `SMOKE PASS`, 61 `[ok ]` checks, durable totals 900 / 60 / 960 ([transcript](receipts/live-panel/smoke-transcript.txt)) |
| `PYTHONPATH=src .venv/bin/python scripts/smoke_frontend.py` | 0 | `FRONTEND SMOKE PASSED`, live phase, `screenshots_written 9` (delivery-3 set) plus 6 live screenshots in `docs/receipts/live-panel/`, `console_errors 0` ([transcript](receipts/live-panel/smoke-frontend-transcript.txt)) |
| `PYTHONPATH=src .venv/bin/python scripts/qualify_image.py --docs-check` | 0 | `checks=50 failures=0` |
| `PYTHONPATH=src .venv/bin/python /tmp/live_cadence_receipt.py` (disposable) | 0 | 10.00 s window under `serve`: 12 `/stats` + 12 `/metrics` hits; `/api/v1/live` 200 with the AC1 values; `/health/ready` 200; SIGTERM exit 0 in 0.26 s ([receipt](receipts/live-panel/live-cadence.txt)) |
| `PYTHONPATH=src .venv/bin/python /tmp/live_only_cadence.py` (disposable) | 0 | 20.00 s, live poller only: 21 + 21 hits (1.05/s), clean join, `state=live`, MTP 5 s `0.702` ([receipt](receipts/live-panel/live-cadence-live-only.txt)) |
| fixture payload probe (disposable) | 0 | representative size: `/stats` 55,583 B, `/metrics` 3,872 B, 48 × 128 expert arrays, 33 histogram lines; defaults byte-identical ([receipt](receipts/live-panel/fixture-payload-sizes.txt)) |
| `PYTHONPATH=src .venv/bin/python scripts/qualify_resources.py` | — | NOT EXECUTED (§4) |

The disposable probes are copied into
[`probes/`](receipts/live-panel/probes/) (`live_cadence_receipt.py`,
`live_only_cadence.py`, `probe_fixture.py`); their outputs are the receipts above. They are
tools, not part of the shipped package.

## 6. Deviations from the plan

- **`schema_version` stays 1.** The plan's D5 example shows `2`, while its own sentence
  says the field "reuses the existing constant". The frozen envelope contract
  (`tests/test_api.py::envelope_ok`, every route) asserts 1, and the payload shape is
  additive, so bumping it would have invalidated the existing contract for no benefit.
- **MTP lifetime in the smoke is `70%`, not the plan's `66%`.** The fixture advances
  accepted/drafted at exactly 7/10 from zero, so lifetime equals the window value.
  `lifetime` is covered independently by `::test_lifetime_uses_the_latest_cumulative_totals`
  with distinct cumulative totals.
- **MoE 5 s fills one poll later than MTP** in the browser phase: the AC1 check edits
  `running` twice, which legitimately ends the stats run for ~1 s each time. The smoke now
  waits on each family's own window instead of asserting both at the same instant
  (`live_5s_window_seconds` 6.1 for MTP, MoE asserted on its own wait).
- **The shared browser smoke regenerates `docs/receipts/delivery-3/*.png`** on every run;
  the nine delivery-3 screenshots now include the live row. They are harness-owned, and
  regenerating them keeps them truthful for the current tree.

## 7. Operator gates that remain

1. Confirm the live `/stats` of the deployed Radiance build exposes `experts.routed` and
   `experts.routed_res` with exactly that spelling for the Jarvis MoE model. Absence is a
   legal `-`, so the panel cannot distinguish "no MoE" from "field missing".
2. Confirm live `/metrics` exposes `radiance:draft_tokens_total` and
   `radiance:draft_accepted_total` with **exactly one** label set. Two label sets make the
   MTP group unavailable by design (specification D3), never a sum.
3. Record the real `/stats` and `/metrics` payload sizes on Jarvis and compare them with
   the fixture's 55,583 B / 3,872 B; the 0.5 CPU budget is unmeasured (§4).
4. Run the resource gate (§4) on a Docker/cgroup v2 host, then the Unraid observation at
   390 px with 1 Hz polling over the LAN.
