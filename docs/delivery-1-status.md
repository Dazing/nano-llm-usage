# Delivery 1 status — durable collector (implemented)

Delivery 1 (durable collector, accounting, storage, lifecycle) is implemented
and verified locally. No API, dashboard, container, backup CLI, memory-fit or
live Jarvis/Unraid claims are made here — those belong to Deliveries 2–4
(see [delivery-map.md](delivery-map.md)).

## Executed environment

- Linux x86_64, [uv](https://docs.astral.sh/uv/) 0.13.5, CPython 3.13.17 (uv-managed)
- SQLite 3.53.1 (via Python stdlib `sqlite3`), WAL mode
- prometheus-client 0.26.0 (exposition parsing), pytest 9.1.1
- Dependencies locked in `uv.lock`; install: `uv sync --frozen`

## Commands

| Purpose | Command |
| --- | --- |
| Install | `uv sync --frozen` |
| Unit/integration tests | `.venv/bin/pytest` (92 tests) |
| End-to-end smoke | `.venv/bin/python scripts/smoke.py` (~3 min, binds 127.0.0.1) |
| Run collector | `.venv/bin/jarvis-dashboard collect` |

## Collector configuration (environment variables)

| Variable | Default | Constraint |
| --- | --- | --- |
| `RADIANCE_BASE_URL` | `http://192.168.1.228:8000` | origin-only http(s) URL, no path/credentials/query/fragment |
| `SOURCE_ID` | `jarvis` | `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` |
| `POLL_INTERVAL_SECONDS` | `15` | 5–300 |
| `HTTP_TIMEOUT_SECONDS` | `3.0` | > 0, < poll interval |
| `DATABASE_PATH` | `/data/dashboard.sqlite3` | any writable path (use a volume in Docker) |
| `RAW_RETENTION_DAYS` | `30` | 1–365 |
| `PORT` | `8080` | 1–65535 (reserved for Delivery 2 API; unused by collector) |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |

Invalid values abort startup with exit code 2 and an actionable message.
All timestamps are UTC; ISO strings are fixed-width and sortable.

## Storage, locking, migration, retention

- Single-writer model: an exclusive `flock` on `<DATABASE_PATH>.lock` guards
  the volume; a second collector refuses to start (exit code 3, payload-free
  `refusing to start` log line).
- A poll is applied in one `BEGIN IMMEDIATE` transaction: samples, hourly
  totals, gap spans, epochs, series state and runtime state commit together or
  not at all; baselines never advance on failed writes (disk-full, locked and
  read-only storage are proven non-advancing in tests).
- Startup refuses: files that are not SQLite, valid SQLite without the
  dashboard schema, databases failing `integrity_check`, and schema versions
  newer than the binary. Each applied migration first writes
  `<db>.pre-migration-<N>.sqlite3` (online-backup API) and verifies it.
- Gap materialization is batched (500 hourly pieces per poll) through a
  resumable `maintenance_cursor` queue; a restart mid-materialization resumes
  without overlapping per-hour coverage.
- Retention (every 24 h of collector uptime, and via `storage.purge_expired`)
  removes raw samples/events past `RAW_RETENTION_DAYS`; hourly totals,
  coverage/quality flags, gap spans, epochs and baselines are kept.
- Replay protection: each application is keyed by
  `(source_id, identity, applied_key)`; replaying a committed poll adds
  nothing, while a repeated counter value under a new key stores an observed
  zero (never treated as a duplicate).

## Smoke receipts (executed 2026-10-25, `scripts/smoke.py`, all ok)

- Normal + restart: totals 900 / 60 / 960; SIGTERM exit code 0 in 0.03 s;
  restart added 0; observed-zero samples recorded; optional request_input
  total 1000 stayed separate from the engine-only primary.
- Gap recovery: 300 credited exactly once with `collection_gap` +
  approximate-attribution flags, never rate-eligible, not re-credited;
  disjoint uptime evidence (restart across the gap) credited nothing and
  recorded `restart_evidence` epochs; midnight split covered in unit tests.
- Crash: `SIGKILL` mid-poll left zero partial rows (`integrity_check` ok);
  restart resumed collection.
- Ownership: second collector exit 3; corrupt copy exit 3; foreign file exit 3.
- Endpoint change under the same source: `endpoint_change` epoch, opening
  balance excluded, lifetime totals preserved; retention removed 13 raw rows
  and kept every hourly row.
- Limits/lifecycle: oversized response bounded as `response_too_large` with
  no fabricated series; stats-only outage credited deltas with
  `continuity_unverified`; missed schedule slots are skipped, never burst
  (unit-tested).

## Known boundaries

- The collector completes the in-flight poll before exiting on SIGTERM
  (within the 15 s budget); it does not abort mid-request.
- Rate-eligible samples require an unbroken engine-0 uptime evidence chain;
  after restarts, gaps, stats outages or unverified continuity the deltas are
  still credited but excluded from rates.
- Delivery 2 must add the read API/OpenAPI, backup/restore CLI, container
  packaging and readiness surfaces on top of `collector_state` runtime keys
  (`storage_status`, `collector_status`, `last_poll_at`, `last_error_code`).
