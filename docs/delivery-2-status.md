# Delivery 2 status — bounded API, service lifecycle, recovery, packaging (implemented)

Delivery 2 delivers the read API and its frozen OpenAPI, the `serve` service
with reliable lifecycle health, durable API prerequisites (schema version 2),
online backup and stopped-volume restore, and the backend-only container
packaging. Verified locally against the controlled fixture. No frontend
(Delivery 3), no resource qualification (Delivery 4) and no live
Jarvis/Unraid claim is made here — see [delivery-map.md](delivery-map.md).

## Executed environment

- Linux x86_64, [uv](https://docs.astral.sh/uv/) 0.13.5, CPython 3.13.17 (uv-managed)
- SQLite 3.53.1 (stdlib `sqlite3`), WAL mode
- fastapi 0.162.0, uvicorn 0.54.0, starlette 1.10.0, pydantic 2.14.1,
  prometheus-client 0.26.0, pytest 9.1.1 (httpx 0.28.1 is a dev-only extra)
- Dependencies locked in `uv.lock`; install: `uv sync --frozen`
- Docker/Podman were **not available** in this environment, so image build and
  container limits are authored and locally validated but not executed here
  (see "Container evidence" below).

## Commands

| Purpose | Command |
| --- | --- |
| Install | `uv sync --frozen` |
| Unit/integration tests | `.venv/bin/pytest` (210 tests, ~31 s) |
| End-to-end smoke | `.venv/bin/python scripts/smoke.py` (~3.5 min, binds 127.0.0.1) |
| Collect only | `.venv/bin/jarvis-dashboard collect` |
| Collect + API | `.venv/bin/jarvis-dashboard serve` |
| Online backup | `.venv/bin/jarvis-dashboard backup --output PATH` |
| Stopped-volume restore | `.venv/bin/jarvis-dashboard restore --input PATH` |
| OpenAPI document | `GET http://127.0.0.1:8080/api/v1/openapi.json` |

Exit codes: `0` clean, `1` unexpected runtime/thread failure, `2` invalid
configuration, `3` storage/ownership/backup/restore refusal.

New configuration: `JARVIS_BIND_ADDRESS` (default `127.0.0.1`, numeric IPv4/IPv6
only, hostnames rejected). `PORT` (default 8080) is now used by `serve`.
`RADIANCE_BASE_URL`, `SOURCE_ID`, `POLL_INTERVAL_SECONDS`,
`HTTP_TIMEOUT_SECONDS`, `DATABASE_PATH`, `RAW_RETENTION_DAYS`, `LOG_LEVEL`
keep the Delivery 1 meanings.

## Wire contract (schema version 1)

Every response — success, error and health — is generated from the pydantic
models in `src/jarvis_dashboard/schemas.py`, carries `schema_version: 1` and a
per-request UTC `generated_at`, and is sent with `Cache-Control: no-store`.

| Route | Bounds | Notes |
| --- | --- | --- |
| `GET /health/live` | — | 200 while the HTTP loop serves; no storage/upstream query |
| `GET /health/ready` | — | 200 only with validated writable storage + live writer thread; 503 with a code (`starting`, `shutting_down`, `storage_*`) |
| `GET /api/v1/status` | — | source id, poll/retention settings, attempt + primary-success recency, storage and collection state; never endpoint credentials or payloads |
| `GET /api/v1/series` | `limit` 1–100, default 100 | keyset cursor over a migration-backed `seq` |
| `GET /api/v1/summary` | `series_id` required, `epoch_limit` 1–20 | `today`/`month`/`lifetime`, latest rates, epoch page (`epoch_cursor`) |
| `GET /api/v1/history` | `series_id`, `from`, `to`, `bucket` | hour: ≤744 buckets (31 d), day: ≤366 buckets (366 d); every bucket generated |
| `GET /api/v1/events` | `limit` 1–100, default 20 | descending `(occurred_at, event_id)` keyset, retention disclosure |

Contract rules enforced by tests and the smoke runner:

- Token values, balances and processed totals are non-negative decimal
  **strings** (or `null`); interval counts are JSON integers; durations and
  rates are finite numbers with declared units. Sums use checked signed-int64
  addition — overflow is reported (`counter_overflow`), never coerced to float
  or wrapped.
- `processed_total` exists only when both primary counters have observed
  intervals and never includes request counters.
- `series_id` is `v1.` + URL-safe base64 of the JSON pair
  `[source_id, canonical_identity]` with a canonical round-trip check. A valid
  id for an unknown series is 404 `series_not_found`; a malformed id is 400
  `invalid_series_id`. Identity is an identifier, not authorization.
- Times are RFC 3339 UTC (`Z` or `+00:00`); naive, non-zero-offset, inverted,
  unaligned and oversized ranges are 400s. Ranges are half-open: a bucket
  starting exactly at `to` is excluded. Nothing is clamped.
- Cursors are versioned, length-bounded, strictly decoded, and bound to
  route + series/filter + ordering; reuse on another route or filter is 400
  `invalid_cursor`. Pages are stable under concurrent inserts (keyset, no
  `OFFSET`).
- Validation problems normalize to the common 400 model (the document advertises
  no `422`/`HTTPValidationError`), unknown routes are 404, unexpected errors are
  a payload-free generic 500 `internal_error` (bounded log reference, no
  traceback). No wildcard CORS.

## Bounded reads

`api.py` admits at most **4** concurrent database reads. The slot is acquired
in the async boundary without waiting, so a 5th request is answered
immediately with 503 `read_capacity_exceeded` and never queues; the slot is
released in a `finally` that also covers cancellation. Each admitted request
opens its own SQLite `mode=ro` URI connection in a worker thread with
`query_only`, a 2 s busy timeout, a 4 MiB cache, mmap disabled, one read
transaction for the whole response, closed before the slot is released. Health
uses the in-memory lifecycle record (not a 5th connection); backup uses its own
single maintenance connection.

## Durable prerequisites added (schema version 2)

Migration `0002_api_metadata.sql` (0001 is never edited) adds per-counter
`first_observed_at`/`last_observed_at`/`metadata_complete` on `hourly_totals`,
a `latest_interval` table, series `seq`/`first_valid_at`/`last_valid_at`,
source `first_success_at`/`last_success_at`, and the indexes the bounded
queries need. The writer now records contributing observations, merges
contribution quality into the hourly row, propagates gap markers to every
counter of the gap's scope (previously all markers landed on
`computed_input`), and stores latest-interval diagnostics.

Legacy rows are rebuilt by a **bounded, resumable, total-neutral** backfill
(state in `maintenance_cursor`, batch 400, run in small rounds at startup).
Where raw samples were already aged out, bounds stay `NULL` and
`metadata_complete: false` — coverage is never invented.
`metadata_complete` is a coverage field, not one of the six quality flags.

## Lifecycle and health

`serve` runs one Uvicorn worker (no reload) plus a dedicated writer thread that
owns the volume lock, the writer connection and collection. The connection is
never created on the main thread. Startup validates real writability (an
`INSERT`+`ROLLBACK` probe — `BEGIN IMMEDIATE` alone succeeds on a WAL volume
whose main file is read-only) and only then publishes readiness; a second
`collect`/`serve` instance refuses (exit 3, `volume_locked`) instead of waiting.
Storage/collector failures are published to the in-memory record **before** the
durable error write, so a failing disk still fails readiness. Unexpected writer
thread death fails readiness and terminates the service with a nonzero exit.
Jarvis being down never makes readiness fail: `collector_status` goes
`starting → healthy/degraded/offline` while `storage_status` stays `ok`.
SIGTERM/SIGINT: readiness disabled → scheduling stopped → active write
finished or rolled back → connections closed → thread joined; measured
0.26 s (`serve`) and 0.11 s (`collect`), budget 15 s.

## Backup and restore

`backup` opens the running volume without initializing, migrating or taking an
exclusive lock, copies committed WAL through the online backup API in bounded
page steps, verifies `PRAGMA integrity_check` on the destination, and ships a
checkpointed snapshot (no `-wal` beside it). It refuses existing paths,
symlinks and source/destination aliasing; a mid-copy failure removes only its
own new file and exits nonzero. Migration snapshots use the same primitive and
refuse to overwrite an existing snapshot (`snapshot_exists`).

`restore` is an offline operation: it takes the target volume lock (a running
target is refused), validates input integrity and schema before touching the
destination, moves the previous main/WAL/SHM set together into a new
non-overwriting `*.pre-restore-<UTC>` directory, installs through a staging
file on the target filesystem, and records one source-wide `restore` event with
provenance (snapshot path, digest, page count, schema, previous set). A failed
install or a failed recovery event rolls the previous set back. Newer-schema
input is refused; nothing ever starts from a silently created empty database.

## Container evidence

`Dockerfile` (two stages, `uv 0.13.5` and `python:3.13.17-slim` pinned by
digest), `compose.yaml`, `.dockerignore` and `scripts/container_healthcheck.py`
implement the required constraints: non-root numeric UID/GID build args,
read-only root, size-limited `/tmp` tmpfs, one `${JARVIS_APPDATA_DIR}:/data`
bind, `${DASHBOARD_BIND_ADDRESS:?…}` host binding (never implicit 0.0.0.0),
`mem_limit: 256m`, `cpus: 0.5`, `pids_limit: 128`, `cap_drop: [ALL]`,
`no-new-privileges`, `stop_grace_period: 15s`, rotated bounded logs, stdlib
healthcheck (no curl/wget), bytecode writing disabled. Operations runbook:
[deployment.md](deployment.md).

Locally executed instead of container-executed: `uv export --frozen` install,
`uv build --wheel` (migrations `0001`+`0002` inside, no tests/caches), a clean
venv install where `jarvis-dashboard serve` became ready, served
`/api/v1/status`, answered `/` with an ordinary 404, wrote a verified backup,
restored it into a fresh volume and exited 0 on SIGTERM with no bytecode files
in the installed package; `compose.yaml` parsed and checked for every required
key. **Not executed** (no container runtime here): `docker build`,
`docker compose up`, and real enforcement of 256 MiB / 0.5 CPU / read-only root
— those belong to Delivery 4 and the operator gate.

## Smoke receipts (executed 2026-10-25, `scripts/smoke.py`, 61 checks + 32 receipts, all ok)

- Scenarios 1–6 (Delivery 1 regressions, unchanged results): 900 / 60 / 960,
  SIGTERM 0.11 s, lock re-acquired after exit, gap recovery credited once,
  `SIGKILL` mid-poll left no partial rows, second collector exit 3
  (`storage failure: database_not_mine` for a foreign file), endpoint change
  preserved lifetime, retention removed 13 raw rows and kept every hourly row,
  `response_too_large` bounded, stats-only outage credited with
  `continuity_unverified`.
- Real `serve` over HTTP: readiness first observed as not-listening, ready in
  0.52 s, `/health/live` 200 during startup; `/api/v1/series` 200 with
  `Cache-Control: no-store`; `/api/v1/summary` lifetime
  `"900"` / `"60"` / processed `"960"`, `schema_version: 1`,
  `collection_status: healthy`; `/api/v1/history` generated every bucket in
  range and summed to `"900"`; a real counter reset produced epochs
  `['counter_reset', 'new_series']` (newest first) and a `counter_reset` event
  on `/api/v1/events`; `openapi.json` exposed 7 paths; `GET /` → `not_found`.
- Bounded reads: 32 concurrent `/api/v1/status` produced only 200 and 503,
  8 served / 24 refused, every refusal `read_capacity_exceeded`, and the next
  request was served (no queue, no wedge). Two focused HTTP checks back this
  up: a lifetime sum of `900 + 9007199254740991 + 2` is returned as the exact
  decimal string `9007199254741893` (never a float), and every API response
  opens exactly one read snapshot and closes it before the slot is released.
- Retention vs API: after `purge_expired` removed the raw rows, lifetime stayed
  `"900"` and history kept `metadata_complete: true` with non-null
  first/last observations.
- Shutdown: `serve` exited 0 on SIGTERM in 0.26 s.
- Backup/restore: backup exit 0 (`schema 2, 34 pages`) while the live volume
  had a `-wal`; the snapshot shipped without one; a second backup exited 3
  (`output_exists`); restore into a fresh stopped volume exited 0
  (`restored 34 pages (schema 2)`), lifetime 900 / generated 60 matched the
  snapshot, exactly one source-wide `restore` event with provenance keys
  `['digest', 'page_count', 'previous_set', 'schema_version', 'snapshot']`;
  restore against a running target exited 3; continuation on the restored
  volume added one interval only (computed 900 → 1000, generated 60 → 70,
  optionals unchanged).

## Test inventory (210 tests)

`test_api` 23, `test_health_api` 13, `test_queries` 22, `test_lifecycle` 8,
`test_service` 9, `test_backup` 11, `test_restore` 10,
`test_metadata_migration` 13 (new); `test_storage` 11, `test_collector` 11,
`test_accounting` 15, `test_config` 12, `test_gaps` 6, `test_metrics_parser` 17,
`test_transport` 9 (Delivery 1, extended where contracts changed).
`tests/asgi_client.py` drives the ASGI app directly (Starlette 1.10's
`TestClient` hangs on this interpreter); `tests/test_storage_helpers.py`
provides shared fixtures, including a real-writer-then-downgrade v1 volume used
to prove the migration and backfill.

## Known boundaries

- `GET /` is an ordinary 404 until the Delivery 3 frontend is bundled; no
  placeholder UI is served.
- Collection status is process-local for the `starting → offline` startup rule;
  recency of data is always derived from durable timestamps.
- Container limits and the 256 MiB / 0.5 CPU envelope are authored, not
  measured; measurement is Delivery 4 (MAP3 keeps local evidence separate from
  the operator gate).
- Live Jarvis/Unraid behaviour (real labels, uptime chain, pool storage,
  private binding) remains the authorized operator gate.
