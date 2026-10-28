# Deployment and operations

One container runs the collector writer, the bounded read API, and the dashboard
UI built at image-build time from `frontend/`. `GET /` serves the dashboard;
the API is under `/api/v1` and health under `/health`. See
[Delivery 3 status](delivery-3-status.md) for the backend/UI evidence and
[Delivery 4 status](delivery-4-status.md) for release qualification: which gates
were executed, on which image ID, and which gates are pending an operator.

## One writer owns the volume

The SQLite volume is owned by exactly one process (`flock` on
`dashboard.sqlite3.lock`). Never run two `serve` (or `collect`) instances
against the same appdata directory, never scale the service above one replica,
and do not put the volume on a shared filesystem. A second instance exits with
code `3` and logs `storage failure: volume_locked`; it does not queue and wait.

Storage is created on local block storage only:

- Pre-create the appdata directory on a primary-storage pool path that is a
  local filesystem (see [Unraid](#unraid) for the NAS shape).
- Not SMB, not NFS, not a FUSE mount, not a mover/sync target: SQLite locking
  and WAL durability are unreliable there.
- Owner must equal the numeric UID/GID the container runs as (see
  [Runtime user](#runtime-user)). Qualification uses
  `1000:1000`; the image default is `10001:10001`:

```bash
export JARVIS_UID=1000 JARVIS_GID=1000
sudo install -d -o "$JARVIS_UID" -g "$JARVIS_GID" -m 0750 /mnt/cache/appdata/jarvis-token-dashboard
```

`chmod 777` is not required anywhere and must not be used: the app writes only
inside `/data`, as its own UID/GID.

## Disk footprint

`RAW_RETENTION_DAYS` is the only time-bounded term an operator controls: detailed
samples and events are deleted past the cutoff (one maintenance pass per 24 h of
collector uptime — samples in batches of 500 rows, events in one pass), while
hourly totals, epochs, baselines and collector state grow for the life of the
volume.

Measured on a schema-faithful volume at the default 15 s poll with every counter
observed and every index counted (`VACUUM INTO` snapshot + `dbstat`):

| Series | 30 days | 180 days (default) |
| --- | --- | --- |
| 1 | 66 MiB | ~400 MiB |
| 8 | 534 MiB | ~3.2 GiB |
| 16 | 1.1 GiB | ~6.5 GiB |

That is ~400 B per retained sample (~2.25 MiB per series per day), so raw volume
is `series × days × (86400 / POLL_INTERVAL_SECONDS) × 400 B` and scales linearly
with both the retention window and the poll rate. The unbounded hourly rollup is
the small term: ~115 MiB per year at eight series. Long label sets are the
biggest multiplier, because `identity` is repeated in the `samples` table and in
all three of its indexes: a 118-byte identity costs ~640 B per sample (+61%).

Size the appdata directory for the window you run, plus the transient `-wal`
sibling and one snapshot backup, which is roughly a full copy — see
[Backup](#backup). Raising `RAW_RETENTION_DAYS` on a running volume only protects
data from that point on: samples already purged under a shorter window are not
recovered. Lowering it takes effect on the next daily maintenance pass, and the
freed pages stay in the file — nothing runs a periodic `VACUUM`, and the backup
API copies pages as they are, so reclaiming that space means an explicit `VACUUM`
with the container stopped. When the volume is tight, lower `RAW_RETENTION_DAYS`
(for example `30`): lifetime,
monthly and daily answers keep coming from the hourly rollups and quality flags,
and only detailed sample/event drill-down narrows.

## Runtime user

The image bakes `APP_UID=10001` / `APP_GID=10001` as a fallback, and
`compose.yaml` sets the **runtime** user explicitly:

```yaml
user: "${JARVIS_UID:?set the numeric appdata owner UID}:${JARVIS_GID:?set the numeric appdata owner GID}"
```

The UID/GID is passed to the container at run time, not baked at build time.
That is deliberate: changing the appdata owner needs no image rebuild, and the
same image ID can be qualified with any numeric identity. The image does create
a passwd entry for its default `10001:10001` (so build-time `chown` works), but
the runtime user usually has none — Python and SQLite use numeric IDs, so no
entry is needed, and the appdata path comes from `DATABASE_PATH`, not from a
home directory. Two consequences:

- The appdata directory must be owned by (or group-writable for) that
  UID/GID — see the `install -d` line above. `user: root` is never required and
  is not supported: `/` is read-only and `/data` is the only writable path.
- `docker compose exec` and one-off `docker run` helpers must run as the same
  UID/GID, otherwise files they create (backups, restored databases) end up
  owned by someone else and the service cannot write them. The restore command
  below passes `--user "$JARVIS_UID:$JARVIS_GID"` for that reason.

`JARVIS_UID` / `JARVIS_GID` must be numeric (`compose.yaml` has no way to
resolve a name), and they must not be `0`.

## Configuration: a `.env` file next to `compose.yaml`

Every Compose command in this document is run from the directory that holds
`compose.yaml`, with the settings in a plain `.env` file beside it. Docker
Compose v2 reads `.env` from that directory automatically, so `stop`, `exec`,
`ps` and `down` find the same values as `up`; exporting them in a shell only
helps that shell. `.env` is not committed: copy `.env.example` and fill it in.

```bash
# .env — same directory as compose.yaml. chmod 0600: it holds no secrets,
# but it names your internal addresses.
DASHBOARD_BIND_ADDRESS=192.168.1.50
DASHBOARD_HOST_PORT=8080
JARVIS_APPDATA_DIR=/mnt/cache/appdata/jarvis-token-dashboard
JARVIS_IMAGE=ghcr.io/dazing/nano-llm-usage@sha256:<digest>
JARVIS_UID=1000
JARVIS_GID=1000
RADIANCE_BASE_URL=http://192.168.1.228:8000
```

| Setting | Meaning |
| --- | --- |
| `DASHBOARD_BIND_ADDRESS` | Required. The trusted LAN address the host publishes on (`0.0.0.0` is never implied). |
| `DASHBOARD_HOST_PORT` | Published host port, default `8080`. |
| `JARVIS_APPDATA_DIR` | Required. Local-storage directory mounted at `/data`. |
| `JARVIS_IMAGE` | Image to run, default `jarvis-dashboard:0.1.0` (the tag `docker compose build` creates). A deployed host sets the digest-pinned GHCR reference; see [Unraid](#unraid). |
| `JARVIS_UID` / `JARVIS_GID` | Required. Numeric user the container runs as; must own the appdata directory. |
| `RADIANCE_BASE_URL` | Required. Origin-only Jarvis base URL, e.g. `http://192.168.1.228:8000`. |
| `JARVIS_SOURCE_ID` | Optional label for the collected series, default `jarvis`. Written into the database, not into the image; changing it starts a new series identity. |
| `POLL_INTERVAL_SECONDS` | Optional, default `15`, accepted range `5..300`. |
| `RAW_RETENTION_DAYS` | Optional, default `180` (six months), accepted range `1..365`. Only detailed samples/events expire; see [Disk footprint](#disk-footprint). |
| `HTTP_TIMEOUT_SECONDS`, `LOG_LEVEL` | Optional tuning; validated at startup, invalid values exit `2`. |
| `COST_RATE_INPUT_PER_MTOK`, `COST_RATE_CACHED_INPUT_PER_MTOK`, `COST_RATE_OUTPUT_PER_MTOK` | Optional. List price per million tokens for the API-equivalent estimate, in USD. Set all three together or leave all three empty; a partial set exits `2` naming every missing variable. See [Cost rates](#cost-rates-the-api-equivalent-estimate). |

Inside the container the API binds the container's own interface on the bridge
network; reachability is decided by the published
`DASHBOARD_BIND_ADDRESS:DASHBOARD_HOST_PORT` mapping. Keep that address on a
private network: the dashboard has **no login and no TLS** of its own. Anyone
who can reach the published port can read every collected series.

```bash
docker compose build         # local only; a deployed host runs `docker compose pull`
docker compose up -d
docker compose ps            # health: starting -> healthy once the writer is ready
```

The container is read-only at `/`, non-root, `cap_drop: ALL`,
`no-new-privileges`, `mem_limit: 536870912` (512 MiB), `cpus: 0.5`,
`pids_limit: 128`, `stop_grace_period: 15s`, a 32 MiB `/tmp` tmpfs, and
`json-file` logs capped at 3 × 5 MiB. The image ships no Node, no build tools,
no dev/reload server, no docker socket, and no GPU access.

While `serve` runs, the dashboard adds a fixed 1 Hz pair of requests
(`/stats` then `/metrics`) to `RADIANCE_BASE_URL` for the live row, on top of
the collector's one request per `POLL_INTERVAL_SECONDS`. That traffic is
read-only and consumes no inference capacity, but it is continuous: budget two
requests per second against the engine, and expect roughly 86 400 pairs per
day. There is no switch to disable it; stopping `serve` stops it.

Health uses `container-healthcheck` (Python standard library, no `curl`):
exit `0` only when `/health/ready` returns `200` with `status: "ready"`. A
Jarvis outage makes the collector status `degraded`/`offline` and leaves
readiness `200`, so the container keeps serving history; storage loss makes it
`503` and the container is marked unhealthy and restarted.

## The dashboard: build, serving and caching

The dashboard is a build-time bundle, not a runtime dependency:

- The `frontend` stage of the Dockerfile runs `npm ci` and `npm run verify`
  (type generation, typecheck, production build) from the committed
  `package-lock.json`; the runtime stage copies the result to `/opt/static` and
  sets `STATIC_DIR=/opt/static`. Node.js is needed only during the build — the
  runtime image has no Node, no build tools and no shell tooling.
- `STATIC_DIR` is baked into the image and is **not** an operator setting. It
  exists for running the app outside the image; it must point at a directory
  containing `index.html` (the built default is `src/jarvis_dashboard/static`).
  With no built assets present the app is API-only and `GET /` is a JSON `404`.
- Open `http://<DASHBOARD_BIND_ADDRESS>:<DASHBOARD_HOST_PORT>/` (default port
  `8080`) in a browser; the JSON API is at the same origin under `/api/v1`.
  `docs/open-dashboard-app.sh [URL]` opens it in a Chromium app window (no tabs or
  address bar), reading the address from the repository `.env`; set `CHROMIUM`
  to use another Chromium-based binary.
- Browser traffic is same-origin only. The bundle contains no CDN host, no web
  font and no analytics, and the browser never contacts Radiance: it talks only
  to this container, which is the sole Radiance client.
- Cache policy: hashed assets under `/assets/` are
  `public, max-age=31536000, immutable`; `index.html` is `no-cache` so a new
  release appears on the next load; every `/api/v1/*` and `/health/*` response
  and every JSON error response is `no-store`.
- The page polls the API every 15 seconds while the tab is visible and pauses
  when the tab is hidden. Server-side, `READ_CAPACITY = 4` admits **four
  concurrent read transactions** across all clients; the fifth concurrent read
  is rejected with `503 read_capacity_exceeded` and retried on the next
  refresh, so a handful of open tabs is well inside the limit.

## Cost rates: the API-equivalent estimate

The dashboard can price what it observed at list prices you supply, so the cache
benefit is visible. The word **API-equivalent** is deliberate: a real provider
invoice carries request counts, cache-write rates, batch discounts and rounding
this app cannot see. Nothing here is an invoice, and no amount is ever stored,
exported, or reconciled against a bill.

```bash
# .env — optional. All three, or none of them.
COST_RATE_INPUT_PER_MTOK=3
COST_RATE_CACHED_INPUT_PER_MTOK=0.30
COST_RATE_OUTPUT_PER_MTOK=15
```

- **All three or none.** One or two of the three is a startup error that names the
  missing variables and exits `2`; the container never starts. Prices that cover
  only part of the model would print a plausible number for the wrong reason.
- **Prices, not data.** Tokens are stored, money is not: no table, column, backup
  or migration contains an amount. Editing the three values and running
  `docker compose up -d` (which recreates the container) reprices the whole stored
  history from the same tokens. Nothing needs to be recollected.
- **Only intervals with cache tracking are priced.** An hour is priced only when
  request input, request output *and* cached input were all observed in it. Hours
  from before cache tracking existed, and hours broken by a collection gap, are
  left out of the amount and out of the cache share; the card then states its
  coverage, e.g. `partial · covers 18.0 of 31.0 days`.
- **Rates unset** removes the dollar line and the footnote only. The cache share
  and the coverage note come from counters, not prices, so they stay readable.

Before trusting an estimate, confirm the source publishes the cache counter, and
treat a source that does not as unpriced rather than free:

```bash
curl -s http://192.168.1.228:8000/metrics | grep -c '^radiance:linear_cached_tokens_total{'
docker compose exec -T dashboard python -c 'import json,urllib.request as u; \
  d=json.load(u.urlopen("http://127.0.0.1:8080/api/v1/summary?series_id=" + \
  json.load(u.urlopen("http://127.0.0.1:8080/api/v1/series"))["items"][0]["series_id"])); \
  c=d["ranges"]["lifetime"]; print(c["counters"]["cached_input"]["tokens"], c["api_equivalent"])'
```

A count of `0` from the first line means the engine publishes no cache counter:
the estimate stays empty (`no data`), `cached_input` stays `null`, and the
diagnostics disclosure lists the `Cached input` scope. `priced_hours` of `0` with
a non-zero `expected_hours` is the same fact from the API side. Never read a zero
amount as "the requests were free".

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

Restore is offline: stop the service, restore, start again. The one-off
container runs as the same UID/GID as the service, and `SOURCE_ID` is passed
under the name the application reads. `docker run` does not read `.env`, so
load it into the shell first (`set -a; . ./.env; set +a`); otherwise the image
falls back to the local `jarvis-dashboard:0.1.0` tag.

```bash
docker compose stop
docker run --rm \
  -v "$JARVIS_APPDATA_DIR:/data" \
  --user "$JARVIS_UID:$JARVIS_GID" \
  -e DATABASE_PATH=/data/dashboard.sqlite3 \
  -e RADIANCE_BASE_URL="$RADIANCE_BASE_URL" \
  -e SOURCE_ID="$JARVIS_SOURCE_ID" \
  --entrypoint jarvis-dashboard "${JARVIS_IMAGE:-jarvis-dashboard:0.1.0}" \
  restore --input /data/backups/dashboard-20261025T120000Z.sqlite3
docker compose up -d
```

Ordering matters and is enforced in code: the snapshot is validated first, the
volume lock is taken (a running target is refused with exit `3` and
`volume_locked`), the existing `main`/`-wal`/`-shm` set is preserved together in
a new `.pre-restore-<timestamp>/` directory, the staged copy is installed with
an atomic rename on the target filesystem, the installed database is
re-verified, and only then a source-wide `restore` event is written with
recovery time and snapshot provenance. Any failure puts the previous set back;
the previous set always stays recoverable and the service never starts against
a silently empty database. Restoring a snapshot from a newer schema version is
refused.

After a restore, the first collection interval continues from the restored
baselines: lifetime totals are identical to the backup, no increment is
duplicated, and the `restore` event is the only accounting-independent
difference.

## Upgrade

1. Back up the running volume (the [Backup](#backup) command) and record the
   lifetime totals from `GET /api/v1/summary` (`ranges.lifetime`).
2. Stop the service:

```bash
docker compose stop
```

3. Put the new image in place: on a deployed host, set `JARVIS_IMAGE` in `.env`
   to the new digest and run `docker compose pull` (see [Unraid](#unraid));
   on a build host, `docker compose build`.
4. Start it:

```bash
docker compose up -d
```

   Startup applies numbered migrations in order and writes its own verified
   `.pre-migration-<N>.sqlite3` first.
5. Confirm `GET /health/ready` is `200` and `/api/v1/status` shows
   `collector_status: "healthy"`, then check the lifetime totals you recorded
   in step 1.

A database from a newer schema version is refused with `schema_version_newer`
— roll back by restoring the step 1 snapshot instead of down-migrating.

Schema version 3 (the API-equivalent estimate) adds two nullable columns,
`cached_input` and `delta_cached_input`, to `samples`. A version 2 volume
therefore takes one `pre-migration-3` snapshot and keeps every pre-cache sample
with `cached_input = null`, which is exactly what leaves those hours unpriced.
Engine and request totals, gap reporting and `processed_total` are unchanged by
the migration — verify the lifetime totals from step 1 are identical, not merely
close. An older image refuses that volume rather than guessing at the columns.

## Unraid

The intended NAS shape is a dedicated appdata share on the **primary** pool:

- Path: `/mnt/<pool>/appdata/jarvis-token-dashboard`, where `<pool>` is the
  cache/SSD pool the share lives on **primary**. Set the share to
  primary-only with **no secondary storage and no mover**: a database (and its
  `-wal`/`-shm` siblings) that migrates between disks under an open file handle
  is a corruption risk, not a tuning detail.
- Never on an SMB/NFS/FUSE mount, and never on a share a mover, snapshot
  replication or backup sync can move while the container is running.
- Prepare the ownership before the first start, as the appdata owner:

```bash
install -d -o "$JARVIS_UID" -g "$JARVIS_GID" -m 0750 "/mnt/cache/appdata/jarvis-token-dashboard"
```

- **Compose v2 is an operator prerequisite.** Unraid ships the Docker Engine
  CLI; install the **Compose Manager** plugin from Community Applications for
  `docker compose` (the v2 plugin, `docker compose version`) and a stack UI. The
  classic Web UI template is not equivalent, because the `user`, `read_only`,
  resource cap, log-cap and `stop_grace_period` settings this document relies on
  live in `compose.yaml`.
- **Bind address and authentication.** Publish on a private address
  (`DASHBOARD_BIND_ADDRESS`, e.g. a `br0`/VLAN address that is not reachable
  from WAN) and keep the container off the reverse proxy: the dashboard has no
  login. If it must sit behind a proxy, add authentication there.
- **The NAS pulls a CI-built image; it never builds.**
  `.github/workflows/image.yml` runs the test suite and builds the Dockerfile
  on every push to `main` and every `v*` tag, then pushes
  `ghcr.io/dazing/nano-llm-usage` tagged `sha-<commit>`, `main`, and the
  version for tags. The run summary prints the digest-pinned reference. Pull
  requests build without pushing.
- **Stack.** In Compose Manager, add a stack, paste `compose.yaml` as its
  compose file and `.env.example` as its `.env`, then fill the values in and pin
  the image by digest:

```bash
JARVIS_IMAGE=ghcr.io/dazing/nano-llm-usage@sha256:<digest from the run summary>
```

  Pin the digest, not `:main`: the running image is then exactly one CI build,
  and rolling back means putting the previous digest back. The stack has no
  build context, so always pull before `up`; with the image present, `up` does
  not build. Compose Manager keeps the stack files under
  `/boot/config/plugins/compose.manager/projects/<stack>/`; run the shell
  commands in this document from there.
- **Registry login.** A package published from a private repository is
  private. Log in once on the NAS with a classic personal access token scoped to
  `read:packages` only; a public package needs no login:

```bash
docker login ghcr.io -u <github-user>
```

- **Start and upgrade.** First start, and every [Upgrade](#upgrade) after
  changing `JARVIS_IMAGE` (back up first: migrations run at startup):

```bash
docker compose pull
docker compose up -d
docker compose ps
```

  Do not let an auto-updater (Watchtower, Unraid's update checker) replace the
  container: the upgrade procedure takes a backup before a new image starts.
- **Qualification.** [Delivery 4 status](delivery-4-status.md) records the ID
  of a locally built image; a CI build is a different image. To keep that gate,
  pull the digest on the qualification host, tag it `jarvis-dashboard:0.1.0`,
  run `scripts/qualify_image.py` without `--build`, and deploy that same digest.
- **Log rotation.** Check the actual caps on the running container:

```bash
docker inspect --format '{{json .HostConfig.LogConfig}}' jarvis-dashboard | python3 -m json.tool
ls -l /var/lib/docker/containers/*/$(docker inspect --format '{{.Id}}' jarvis-dashboard)*.json*
```

  Expected: `Type: json-file`, `Max-Size: 5m`, `Max-File: 3` — at most ~20 MiB
  of logs per container. If the daemon's default log rotation differs, keep the
  `logging:` block from `compose.yaml` (it overrides the daemon default).
- Unraid's Docker pids limit is disabled, so the compose `pids_limit: 128` is
  the effective process cap; `user: 1000:1000` (or your UID/GID) is what owns
  `/data`, not `root`.

## Live check against a real Jarvis (AC8)

This is a read-only observation pass on the target network. It changes nothing:
no restore, no restart of Jarvis, **no Radiance restart** — restarting the real
Jarvis process is out of scope and needs separate authorization.

1. Confirm the collector can see the upstream from the dashboard's network,
   from a host on that network:

```bash
curl -fsS "$RADIANCE_BASE_URL/metrics" | grep -E '^radiance:(prefill_tokens_total|decode_tokens_total)|^vllm:(prompt_tokens_total|generation_tokens_total)'
curl -fsS "$RADIANCE_BASE_URL/stats"
```

   Expect the four metric names (`radiance:prefill_tokens_total`,
   `radiance:decode_tokens_total`, `vllm:prompt_tokens_total`,
   `vllm:generation_tokens_total`) with the labels Jarvis actually exposes
   (`model_name`, `engine`), and a `stats` body carrying `model` and
   `uptime_s`. Labels differ between deployments — the dashboard stores the
   identity it sees, it does not require a fixed label set.
2. Record the raw counters for one identity, then send **one small known
   request directly to Jarvis** with no other traffic, wait two poll intervals,
   and compare the raw counter differences with the dashboard deltas from
   `GET /api/v1/summary` (`ranges.today`) and `GET /api/v1/events`:
   - generated output delta equals the `radiance:decode_tokens_total`
     difference;
   - the prefill delta **can differ from the request's prompt token count**,
     because prefix caching serves part of the prompt from cache; that is
     agreement with the counter, not a loss;
   - request input/output track `vllm:prompt_tokens_total` /
     `vllm:generation_tokens_total`, and are `null` in a bucket where the
     endpoint exposed neither (absence is recorded, never guessed as zero).
3. Restart **only the dashboard collector container** (the same service, e.g.
   `docker compose restart dashboard`) and confirm continuity: lifetime totals
   keep counting from the stored baselines, no increment is duplicated, no new
   epoch opens for the dashboard's own restart, and the gap while it was down
   is recorded as `gap_seconds` with `collection_gap` on the affected bucket.
4. Confirm the operational properties on the target:
   - `docker top dashboard -o user,pid,comm` shows the configured non-root
     UID/GID;
   - the log directory respects the caps above (rotated files exist, total size
     is bounded);
   - `docker compose down` + `up -d` (or a full NAS reboot) leaves the volume
     contents intact and the lifetime totals unchanged;
   - the published address answers from an allowed client on the private
     network and is unreachable from a disallowed one (WAN/other VLAN).

Record the raw responses, the deltas and the image ID alongside this run. Any
mismatch is a finding for the owning area, not something to explain away.

## Resource qualification

The limits in `compose.yaml` are measured, not assumed:
[Delivery 4 status](delivery-4-status.md) records the qualified image ID, the
D7 hard-requirement table (RSS median/max, `memory.peak`, non-reclaimable
maximum, OOM counts, CPU, threads/fds/sockets/timers/canvases, DB/WAL/SHM
sizes) and the growth extrapolation from at least 30 measured minutes. Those
numbers are **local** to the qualification host. Fit on the target NAS remains
an operator gate: the same workload shape on the NAS hardware, with the limits
above left unchanged.
