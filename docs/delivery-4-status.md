# Delivery 4 status — local release qualification

Receipts: [`docs/receipts/delivery-4/`](receipts/delivery-4/). Plan:
[delivery-4-implementation-plan.md](delivery-4-implementation-plan.md).
Date: 2026-10-26 (UTC).

## 1. Summary

- **Outcome:** the qualification harness, the one-year seeder and the workload
  driver are written and wired; the parts of Delivery 4 that this machine can
  execute passed (durable-recovery smoke, Compose/configuration contract,
  documentation consistency, the seeded-year contract verified through the real
  read API, and the AC7 growth math). **Local release qualification is not
  claimed.**
- **Hard blocker:** this host has no container runtime. `docker`, `docker
  compose` v2 and a container engine are absent, and the image gates need root
  for the appdata `chown` and the ENOSPC tmpfs bind. Plan D1 forbids
  substituting a host `serve`/`collect` process for the image, so every gate
  that is defined *on the image* is recorded NOT EXECUTED with its exact
  missing prerequisites (§4).
- **AC status:** AC2 (accounting through a full year), AC4 (API/UI agreement,
  host-side browser half) and AC7's growth math have executed local evidence.
  AC1, AC3, AC5, AC6 and AC8 do not — they are image- or operator-gated (§3).
- **Superseded runs:** three. `20261026T094630Z-seed/` (and its growth
  companion `20261026T102000Z-growth/`) was a full 367-day run whose last 30
  days carried no traffic — a defect in the seeder's traffic model, found in
  review, described in §5. `20261026T102400Z-frontend/` is the same browser
  harness run an hour before the final tree. Each superseded directory carries a
  `SUPERSEDED.md`. The cited runs are `20261026T103455Z-seed/` (1,518 checks, 0
  failures), `20261026T110900Z-growth/` and
  `20261026T111200Z-final-tree/`. A 12-day mechanics volume, used to check the
  seeder's plumbing before the full run, is described in §5 and is not cited as
  evidence.

## 2. Environment (executed gates)

From
[`20261026T100600Z-preflight/environment.json`](receipts/delivery-4/20261026T100600Z-preflight/environment.json):

| Field | Value |
|---|---|
| OS / kernel | `Linux 7.2.11-1-cachyos (x86_64)` |
| CPU | AMD Ryzen 7 1800X Eight-Core Processor, 16 cores |
| Memory | 32,776,308 kB |
| cgroup | v2 unified; controllers `cpuset cpu io memory hugetlb pids rdma misc dmem` |
| appdata filesystem | `overlay` (not a NAS pool) |
| Browser | Chromium 156.0.8078.47 (Debian 12 bookworm) |
| Docker / compose | `docker_version: null` — CLI absent from PATH |
| Git | HEAD `249a741ebc28f77ed408c5d72045de30dc9664ff`, 27 uncommitted paths |
| Locks | `uv.lock` `6a4cbcaafad2…`, `frontend/package-lock.json` `e4b07c85a5ef…` |

No image ID, image size or in-image SQLite/Python/distribution version exists
for this session: the image was never built (§4).

## 3. Acceptance criteria

| AC | Result | Evidence / limitation |
|---|---|---|
| AC1 — canonical series and counter semantics in the final image | **NOT EXECUTED** | `qualify_image.py --inspect` requires the built image. Refusal recorded in [`20261026T100610Z-refusal-build/`](receipts/delivery-4/20261026T100610Z-refusal-build/). |
| AC2 — baselines, zeros, gaps, resets, continuity | **EXECUTED (host)** | Seeded 367-day volume verified through the real `serve` API: 1,518 checks, 0 failures ([`seed/verify.log`](receipts/delivery-4/20261026T103455Z-seed/verify.log)). Image half NOT EXECUTED. |
| AC3 — restart safety, migration, retention in the final image | **PARTIAL** | Retention + migration evidence is host-side: 0 raw samples older than the 30-day cutoff, 775,153 rows purged, `integrity_check ok` ([`seed/seed-receipts.txt`](receipts/delivery-4/20261026T103455Z-seed/seed-receipts.txt)). Cases 2, 5, 6, 8, 9 (volume lock, ENOSPC, schema-1 migration, endpoint change) are image gates: NOT EXECUTED. |
| AC4 — UTC API/UI agreement and responsive states | **EXECUTED (host)** | Final-tree browser smoke on the real `serve`: see §5 "final tree". Production-image browser pass NOT EXECUTED. |
| AC5 — self-contained production image | **NOT EXECUTED** | Needs `docker compose build` + in-container network inspection. |
| AC6 — bounded measured resource workload | **NOT EXECUTED** | `qualify_resources.py` refuses on the D1 prerequisites ([`refusal-workload/`](receipts/delivery-4/20261026T100630Z-refusal-workload/)). No RSS, cgroup, CPU, p95 or continuity numbers exist. |
| AC7 — restore and failed-write safety | **PARTIAL** | Growth math measured (§6). Backup/restore/ENOSPC cases 6–7 are image gates: NOT EXECUTED. |
| AC8 — real metric discovery and known-generation comparison | **PENDING (operator)** | Requires the authorized live Jarvis. The procedure is written in [`deployment.md`](deployment.md) and its command blocks are machine-checked (§5). |

## 4. Image gates: NOT EXECUTED, with exact missing prerequisites

Plan D1: these gates are defined on the image; a host process is not a
substitute. Each script refuses before touching a volume, and the refusal is
recorded (`checks=1 failures=1`, exit 1):

| Gate | Command | State |
|---|---|---|
| Step 5 build + AC1 inspect | `.venv/bin/python scripts/qualify_image.py --build --inspect` | NOT EXECUTED |
| Step 6 cases 1–9 | `.venv/bin/python scripts/qualify_image.py --cases` | NOT EXECUTED |
| Step 7 workload | `.venv/bin/python scripts/qualify_resources.py --duration-minutes 30` | NOT EXECUTED |
| Step 3 fixture reachability through the private gateway | `.venv/bin/python scripts/qualify_common.py --fixture-check` | NOT EXECUTED |

Missing prerequisites, verbatim from
[`20261026T100620Z-refusal-cases/run.log`](receipts/delivery-4/20261026T100620Z-refusal-cases/run.log):

- `docker (the \`docker\` CLI is not on PATH)`
- `docker compose v2 (\`docker compose version\` needs the docker CLI)`
- `a container runtime: install Docker Engine (rootless is acceptable)`
- `root privileges (the harness must chown appdata to $JARVIS_UID:$JARVIS_GID and bind-mount the ENOSPC tmpfs)`

Providing a Docker host and root (or a rootless engine with the appdata
ownership pre-prepared by the operator) is the environment prerequisite named
in the plan's risk list; the harness then runs every gate above end to end.

## 5. Executed gates

| Command | Exit | Result |
|---|---|---|
| `.venv/bin/python -m pytest -q` | 0 | 243 passed |
| `.venv/bin/python scripts/smoke.py` (run 1, transcript) | 0 | `SMOKE PASS` |
| `.venv/bin/python scripts/smoke.py` (runs 2, 3) | 0, 0 | 61 checks each; [`smoke/`](receipts/delivery-4/20261026T093900Z-smoke/) `run-2.log`, `run-3.log`, `exits.txt` |
| final-tree chain: `pytest -q` → `smoke.py` → `smoke_frontend.py` | 0, 0, 0 | 243 passed; 61 checks + `SMOKE PASS` + `restore refuses a running target: 3`; 14 phases / 65 checks + `FRONTEND SMOKE PASSED` ([`final-tree/`](receipts/delivery-4/20261026T111200Z-final-tree/)) |
| `.venv/bin/python scripts/qualify_image.py --docs-check` | 0 | 49 checks, 0 failures ([`docs/`](receipts/delivery-4/20261026T100400Z-docs/)) |
| `.venv/bin/python scripts/qualify_common.py --preflight` | 1 | environment recorded, four missing items named ([`preflight/`](receipts/delivery-4/20261026T100600Z-preflight/)) |
| `.venv/bin/python scripts/seed_history.py --window-days 367 …` | 0 | [`seed/seed-receipts.txt`](receipts/delivery-4/20261026T103455Z-seed/seed-receipts.txt) |
| `.venv/bin/python scripts/seed_history.py --verify` | 0 | 1,518 checks, 0 failures ([`seed/verify.log`](receipts/delivery-4/20261026T103455Z-seed/verify.log)) |
| `.venv/bin/python scripts/seed_history.py --verify` (receipt manifest) | 0 | 1,518 checks, 0 failures — the trimmed receipt manifest alone is sufficient ([`verify-trimmed-manifest.log`](receipts/delivery-4/20261026T103455Z-seed/verify-trimmed-manifest.log)) |
| `.venv/bin/python scripts/qualify_resources.py --growth-only` | 0 | 3 checks, 0 failures ([`growth/`](receipts/delivery-4/20261026T110900Z-growth/)) |

**Step 1 (recovery smoke).** Scenario 8 now waits on the *volume lock*, not on
`has_series()`: `scripts/smoke.py` `volume_lock_held(db)` probes
`dashboard.sqlite3.lock` with `fcntl.flock(LOCK_EX|LOCK_NB)` after the restored
snapshot has already made `has_series()` true. The check
`restore refuses a running target: 3 (expected 3)` passes in all three runs.

**Step 2 (Compose contract).** Parsed from `compose.yaml`: environment keys
`DATABASE_PATH, HTTP_TIMEOUT_SECONDS, JARVIS_BIND_ADDRESS, LOG_LEVEL,
POLL_INTERVAL_SECONDS, PORT, RADIANCE_BASE_URL, RAW_RETENTION_DAYS, SOURCE_ID`;
`SOURCE_ID: "${JARVIS_SOURCE_ID:-jarvis}"` (the product reads `SOURCE_ID`, not
`JARVIS_SOURCE_ID` — `src/jarvis_dashboard/config.py:from_env`);
`user: "${JARVIS_UID:?…}:${JARVIS_GID:?…}"`; no `build.args`;
`mem_limit: 256m`, `cpus: 0.5`, `pids_limit: 128`, `stop_grace_period: 15s`.
Restore `docker run` carries `--user "$JARVIS_UID:$JARVIS_GID"`.

**Step 4 (one-year seed through the real collector).** 269,695 simulated polls
(2,151,800 applied deltas, 720 failed) in 1,766.8 s over a 367-day window
(2025-10-24T00:00:00Z → 2026-10-26T10:34:55Z, 300 s cadence to day −30 then
15 s), 8 series, 1,376,640 raw samples, 282,204 hourly rows, 109 gap spans, 10
epochs (8 `new_series`, 1 `counter_reset`, 1 `restart_evidence`), 1 event,
`integrity_check ok`, 775,153 rows purged, **0 raw samples older than the
30-day retention cutoff**, lifetime `processed_total` 749,186,778.

The verifier starts the real `serve` from the same tree and compares: per-model
and total lifetime per counter (totals 700,184,266 computed_input, 49,002,512
generated_output, 630,118,896 request_input, 41,997,619 request_output); 1,464
daily rows (366 days × 4 counters, including the fully-idle day as `"0"`
strings and the collector-downtime day as `null`); the gap-only hour at the
designed outage (`tokens: null`, `gap_seconds > 0`); the recovered hour with
`collection_gap` + `time_attribution_approximate` and exact per-counter tokens
(2026-10-26T00Z for model 0: 7,159 / 499 / 6,479 / 437 — the whole outage span
credited once); the previous-midnight restart hour (the boundary poll credits
nothing, the polls after it do: 1,216 / 84 / 1,125 / 72) with a
`restart_evidence` epoch; the fixed counter-reset epoch; request counters
absent for seven fixed days; raw retention; integrity.

**Seeder defect found in review, fixed, and re-run.** The first full run
(`20261026T094630Z-seed/`, marked superseded) dropped every per-poll step below
1 token. At the 15 s cadence that freezes all four counters, so its last 30
days were flat and its designed hours were credited `0` — and `--verify` agreed
with the manifest, because the expectation ledger is derived from the same
traffic model, so the checks passed vacuously. Fix: `TOKEN_FLOOR` 1.6 →
240 tokens/minute at weight 1.0 (a shared engine's busy hour) and fractional
accumulation in `TrafficModel.advance`. The fix was proven on a 12-day mechanics
volume first (106 checks, 0 failures, recovered hour 7,678 / 517 / 6,660 / 448
with both flags, previous-midnight 945 / 75 / 1,018 / 57 — non-zero, so the
hourly checks can now fail), then the full year was re-seeded and re-verified
as recorded above. Lesson kept: an expectation generated by the same model as
the code under test cannot see a model defect, so the designed-hour values are
checked for being non-zero, not only for agreement.

**Step 5 (documentation consistency, AC8 half).** `doc_consistency_check()`
extracts the fenced command blocks from [`deployment.md`](deployment.md) and
requires the exact strings the case suite executes: the backup command, the
three restore steps, the five upgrade steps. It also asserts the stale text is
gone (`Backend-only delivery`, `Delivery 9`, `/srv/appdata`, `useradd`), the
`.env`-beside-`compose.yaml` rule, the runtime-user rule, the Unraid rules
(primary pool only, no mover, not SMB/NFS, `install -d -o`, Compose v2 as an
operator prerequisite, image moved by ID with `docker save`/`docker load`, log
rotation), the AC8 live-check steps (four metric names, one small known
request, two polls, prefix-caching caveat, collector-only restart, no Radiance
restart), and the nine Compose rows plus the eight Dockerfile pin rows. Result:
49 checks, 0 failures.

**Final tree.** After every Delivery 4 change was on disk, the three gates ran
in one chain and all exited 0
([`20261026T111200Z-final-tree/`](receipts/delivery-4/20261026T111200Z-final-tree/)):
`pytest -q` → 243 passed. `scripts/smoke.py` → 61 checks, `SMOKE PASS`,
including the Step 1 clause `restore refuses a running target: 3 (expected 3)`;
this is in addition to the three consecutive exit-0 runs of §5. `scripts/smoke_frontend.py` →
14 phases, 65 checks and 0 `FAIL`, driven by Chromium 156 against the real `serve` from this tree at 1440 px and 390 px: API/UI value agreement, range presets and the exclusive custom end, null buckets with one canvas, empty and baseline-only states, disconnect/stale/restart recovery, absent request counters, a single 15 s poll timer (paused when hidden, resumed on focus), theme persistence, reduced motion, visible keyboard focus, the selection-race guard, and same-origin traffic with a clean console. Screenshots: 9 PNGs (desktop 1440 and mobile 390 for normal, offline, stale, empty, dark, custom-range, chart-table, diagnostics), written by that harness to `docs/receipts/delivery-3/` and copied to
[`final-tree/shots/`](receipts/delivery-4/20261026T111200Z-final-tree/shots/).

## 6. AC7 growth math (measured, host-side)

From [`20261026T110900Z-growth/growth.json`](receipts/delivery-4/20261026T110900Z-growth/growth.json),
taken with `VACUUM INTO` on a snapshot (634,585,088 bytes) of the finished
987,324,416-byte 367-day volume, and per-table bytes from `dbstat`, **including
every index of the table** (the index bytes are part of a row's real cost):

| Measure | Value |
|---|---|
| Retained raw samples (30-day window, 8 series, 15 s) | 1,375,728 rows |
| `samples` table + its indexes | 518,201,344 B → **376.67 B/sample** |
| `hourly_totals` + its indexes | 116,228,096 B / 282,236 rows → **411.81 B/hourly row** |
| Hourly rows per day (measured / expected 24 × 4 × 8) | 769.0 / 768 |
| Hourly growth | 316,270 B/day → **115 MB/year, 577 MB/5 y, 1.15 GB/10 y** |
| Bounded raw window | 518 MB, pinned by `RAW_RETENTION_DAYS=30` |

Stated assumptions: raw samples stay bounded by the retention window, so the
extrapolated term is the hourly rollup (one row per counter per hour per
series); the seeded volume's pre-retention raw history is absent by design
(it was purged), so the raw figure is the steady-state 30-day volume, not a
367-day accumulation; per-row bytes include table and index pages, so the
projection is not double-counted elsewhere; the count of series (8) is the
seeded model count and grows with the models a real Jarvis serves, which is the
dominant multiplier on the target host.

## 7. Target misses and tradeoffs

- No D7 target was measured (median RSS, p95 summary, p95 366-day history):
  the workload never ran. There is no value to report, and no limit was raised.
- The 192 MiB non-reclaimable bar, the two-tab cadence check, commit
  continuity and the ledger identity are implemented as hard checks in
  `scripts/qualify_resources.py` and remain untested end to end.
- `docs/deployment.md` documents `/data` appdata on a local pool path example;
  the target-host binding (private address, allowed/disallowed clients) is an
  operator observation, not a local measurement.

## 8. Documentation correction carried from Delivery 3

`docs/delivery-3-status.md:153` states that the server admits **8** concurrent
readers. The product constant is `READ_CAPACITY = 4`
(`src/jarvis_dashboard/api.py:32`); the fifth concurrent read returns
`503 read_capacity_exceeded` (`src/jarvis_dashboard/api.py:92`). The Delivery 3
document is left as the historical record
of that run; [`deployment.md`](deployment.md) now states the correct value, and
the doc-consistency check asserts the phrase `read_capacity = 4` is present.

## 9. Open items

1. **Container-runtime gate** (blocks AC1, AC3-image, AC5, AC6, AC7-cases):
   install Docker Engine (rootless acceptable) with Compose v2, run as root or
   pre-own the appdata to `$JARVIS_UID:$JARVIS_GID`, then
   `qualify_image.py --build --inspect`, `--cases`, `qualify_resources.py
   --duration-minutes 30`. Every number in §3 must then come from
   `docs/receipts/delivery-4/<run-id>/` tied to the built image ID.
2. **Operator gate (AC8, scenario 34):** live metric/label discovery, the
   known-generation comparison, Unraid pool/binding behaviour, and target-host
   AC6 (scenario 33). Requires operator access and authorization; cannot pass
   with fixtures. Radiance restarts need separate authorization.
3. **Cross-browser coverage:** only Chromium 156 is driven
   (`scripts/browser_driver.py` over CDP). Firefox/WebKit need a different
   driver.
4. **Human visual and assistive-technology review:** the ARIA/keyboard checks
   are mechanical; no screen-reader or human review has been performed.
5. **Seeder fidelity:** the synthetic year exercises the read path and the
   retention window, not production traffic shape; the pre-retention raw volume
   is deliberately absent (§6). Traffic is calibrated by `TOKEN_FLOOR = 240`
   tokens/minute at diurnal weight 1.0 for the busiest of 8 models with the
   fixed `COUNTER_RATIO` spread (≈1.9 M computed_input/day, 700 M over the year)
   — a plausible shared-engine shape, chosen so every designed hour moves all
   four counters at the 15 s cadence; it is not a measurement of the operator's
   real Jarvis workload.
