# Deployment and operations

Backend-only delivery: one container runs the collector writer and the bounded
read API. The browser UI arrives in Delivery 3; `GET /` is an ordinary
`404 not_found` JSON response until then. There is deliberately no placeholder
UI in this delivery.

## One writer owns the volume

The SQLite volume is owned by exactly one process (`flock` on
`dashboard.sqlite3.lock`). Never run two `serve` (or `collect`) instances
against the same appdata directory, never scale the service above one replica,
and do not put the volume on a shared filesystem. A second instance exits with
code `3` and logs `storage failure: volume_locked`; it does not queue and wait.

Storage is created on local block storage only:

- Pre-create the appdata directory on a local SSD / cache pool path (for example
  `/srv/appdata/jarvis-dashboard`).
- Not SMB, not NFS, not a FUSE mount, not a mover/sync target: SQLite locking
  and WAL durability are unreliable there.
- Owner must equal the numeric UID/GID baked into the image. The default is
  `10001:10001`; override both so the container matches your appdata owner:

```bash
export JARVIS_UID=10001 JARVIS_GID=10001
sudo install -d -o "$JARVIS_UID" -g "$JARVIS_GID" -m 0750 /srv/appdata/jarvis-dashboard
```

`chmod 777` is not required anywhere and must not be used: the app writes only
inside `/data`, as its own UID/GID.

## Required host settings

`compose.yaml` refuses to start when a host-facing value is missing — nothing
falls back to a wildcard bind.

| Setting | Meaning |
| --- | --- |
| `DASHBOARD_BIND_ADDRESS` | Required. The trusted LAN address the host publishes on (`0.0.0.0` is never implied). |
| `DASHBOARD_HOST_PORT` | Published host port, default `8080`. |
| `JARVIS_APPDATA_DIR` | Required. Local-storage directory mounted at `/data`. |
| `JARVIS_UID` / `JARVIS_GID` | Required. Numeric owner used by `useradd` in the image. |
| `RADIANCE_BASE_URL` | Required. Origin-only Jarvis base URL, e.g. `http://192.168.1.228:8000`. |
| `JARVIS_SOURCE_ID`, `POLL_INTERVAL_SECONDS`, `HTTP_TIMEOUT_SECONDS`, `RAW_RETENTION_DAYS`, `LOG_LEVEL` | Optional tuning; validated at startup, invalid values exit `2`. |

Inside the container `JARVIS_BIND_ADDRESS=0.0.0.0` on purpose: that is the
container's own interface on the bridge network. Reachability is decided by the
published `DASHBOARD_BIND_ADDRESS:DASHBOARD_HOST_PORT` mapping.

```bash
export DASHBOARD_BIND_ADDRESS=192.168.1.50 DASHBOARD_HOST_PORT=8080
export JARVIS_APPDATA_DIR=/srv/appdata/jarvis-dashboard
export RADIANCE_BASE_URL=http://192.168.1.228:8000
docker compose build
docker compose up -d
docker compose ps            # health: starting -> healthy once the writer is ready
```

The container is read-only at `/`, non-root, `cap_drop: ALL`,
`no-new-privileges`, `mem_limit: 256m`, `cpus: 0.5`, `pids_limit: 128`, a
32 MiB `/tmp` tmpfs, `json-file` logs capped at 3 × 5 MiB, and a 15 s stop
grace period. The image ships no Node, no build tools, no dev/reload server, no
docker socket, and no GPU access.

Health uses `container-healthcheck` (Python standard library, no `curl`):
exit `0` only when `/health/ready` returns `200` with `status: "ready"`. A
Jarvis outage makes the collector status `degraded`/`offline` and leaves
readiness `200`, so the container keeps serving history; storage loss makes it
`503` and the container is marked unhealthy and restarted.

## Backup

Online snapshot of a running volume, including the committed WAL, verified with
`PRAGMA integrity_check`, and never overwriting an existing file:

```bash
docker compose exec dashboard jarvis-dashboard backup \
  --output /data/backups/dashboard-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
```

Refusals exit `3` with a code only (no rows, no credentials):
`source_missing`, `source_not_database`, `database_corrupt`,
`schema_version_newer`, `output_alias`, `output_exists`, `output_unwritable`,
`backup_failed`, `snapshot_integrity`. On failure the partial output the command
created is removed; an existing backup is never touched. Every migration takes
the same snapshot path first (`.pre-migration-<N>.sqlite3`) and refuses to run
if that file already exists.

## Restore

Restore is offline: stop the service, restore, start again.

```bash
docker compose stop
docker run --rm \
  -v "$JARVIS_APPDATA_DIR:/data" \
  -e DATABASE_PATH=/data/dashboard.sqlite3 \
  -e RADIANCE_BASE_URL="$RADIANCE_BASE_URL" \
  --entrypoint jarvis-dashboard jarvis-dashboard:0.1.0 \
  restore --input /data/backups/dashboard-20261025T120000Z.sqlite3
docker compose up -d
```

Ordering matters and is enforced in code: the snapshot is validated first, the
volume lock is taken (a running target is refused), the existing
`main`/`-wal`/`-shm` set is preserved together in a new
`.pre-restore-<timestamp>/` directory, the staged copy is installed with an
atomic rename on the target filesystem, the installed database is re-verified,
and only then a source-wide `restore` event is written with recovery time and
snapshot provenance. Any failure puts the previous set back; the previous set
always stays recoverable and the service never starts against a silently empty
database. Restoring a snapshot from a newer schema version is refused.

After a restore, the first collection interval continues from the restored
baselines: lifetime totals are identical to the backup, no increment is
duplicated, and the `restore` event is the only accounting-independent
difference.

## Upgrade

1. `jarvis-dashboard backup --output ...` (or `docker compose exec` the same).
2. Stop the service.
3. Pull/build the new image tag.
4. `docker compose up -d`. Startup applies numbered migrations in order and
   writes its own verified `.pre-migration-<N>.sqlite3` first.
5. Confirm `GET /health/ready` is `200` and `/api/v1/status` shows
   `collector_status: "healthy"`, then check the lifetime totals you recorded
   in step 1.

A database from a newer schema version is refused with `schema_version_newer`
— roll back by restoring the step 1 snapshot instead of down-migrating.

## Resource qualification

The limits above are the intended shape, not a measured qualification. Delivery
9 (`docs/delivery-map.md`) qualifies them under real load: 24 h soak inside
256 MiB, restart-under-pressure, backup-on-SSD timing, and the browser
observations. An early constrained smoke run is not Delivery 9 qualification.
