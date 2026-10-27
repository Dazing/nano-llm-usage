# Jarvis Token Dashboard — portable implementation packet

This repository contains the application specification and the implemented Delivery 1 durable
collector, the Delivery 2 bounded API, service lifecycle, backup/restore and packaging, and the
Delivery 3 dashboard: the production image builds the Vue bundle and serves it from the same origin
as the API, so one container is the whole product. Delivery 4 is release qualification: the harness
and the measured evidence live in [`docs/receipts/delivery-4/`](docs/receipts/delivery-4/) and
[`docs/delivery-4-status.md`](docs/delivery-4-status.md), which states plainly which gates ran and
which the qualification host could not run. The cache-aware API-equivalent cost estimate adds an
optional `cached_input` counter, read-time pricing at operator-supplied rates, and cache-share and
coverage lines in the dashboard. No verified live deployment is claimed. Keep the
specification packet together and preserve its relative links.

## Files and authority

1. **[specification.md](docs/specification.md)** — authoritative product, stack, accounting, data/API/UI, packaging, operations and resource contracts; ordered implementation steps.
2. **[acceptance-scenarios.md](docs/acceptance-scenarios.md)** — normative numeric and failure examples to turn into regression tests and production smoke scenarios.
3. **This README** — context, kickoff prompt and delivery expectations. It does not override the specification.

Selected stack: Python 3.13 + FastAPI/Uvicorn + SQLite; Vue 3 + TypeScript + Vite + Chart.js compiled into static assets. One runtime container, one Python worker, no Node.js runtime. The dashboard is that build-time bundle: `frontend/` is compiled during the image build into `src/jarvis_dashboard/static/` (baked to `/opt/static`), and the running container ships no Node.js, no build tools and no CDN reference. The `serve` process also runs a 1 Hz live poller that feeds the live row (`GET /api/v1/live`) from memory only; no live value is persisted. RAM target: 512 MiB hard limit, preferred steady-state process RSS below 100 MiB; both require measurement.

The user chose a DIY application after considering VictoriaMetrics/Grafana because their Unraid NAS is short on RAM. The goal is durable aggregate token history with an attractive simple dashboard, not per-request logs or a general monitoring platform. Earlier RAM estimates were unmeasured and must not become advertised benchmark results.

## Delivery planning

- **[Delivery map](docs/delivery-map.md)** — four cumulative deliveries, acceptance/scenario ownership, dependencies, evidence gates and the separate authorized Jarvis/Unraid gate.
- **[Delivery 1 implementation plan](docs/delivery-1-implementation-plan.md)** — detailed durable collector/accounting contracts, implementation steps and local HTTP/process verification.
- **[Delivery 1 status](docs/delivery-1-status.md)** — implemented collector: executed environment, install/run/test/smoke commands, configuration, storage/locking/migration/retention behavior and numeric smoke receipts.
- **[Delivery 2 implementation plan](docs/delivery-2-implementation-plan.md)** — bounded HTTP contracts, actual-core prerequisite repairs, non-root runtime, online backup/stopped restore and local verification gates.
- **[Delivery 2 status](docs/delivery-2-status.md)** — implemented API/lifecycle/recovery/packaging: executed environment, commands, wire contract, bounded reads, schema version 2, smoke receipts and the explicitly unexecuted container gates.
- **[Delivery 3 implementation plan](docs/delivery-3-implementation-plan.md)** — contract-generated TypeScript client, dashboard views/states, exact UTC formatting, token system, static-serving/cache split, browser driver and image build stage.
- **[Delivery 3 status](docs/delivery-3-status.md)** — implemented dashboard: executed environment, 65 browser smoke checks, receipts and screenshots, the cache/snapshot contract table, and the explicitly unexecuted image and resource gates.
- **[Delivery 4 implementation plan](docs/delivery-4-implementation-plan.md)** — release qualification of the production image: container harness, one-year history seeder, image end-to-end cases, the specification D9 resource workload and pass/target criteria, packaging/documentation fixes and evidence publication.
- **[Delivery 4 status](docs/delivery-4-status.md)** — qualification results: executed host gates (durable-recovery smoke, Compose/configuration contract, the seeded 367-day year verified through the real API, documentation consistency, AC7 growth math), the image and resource gates recorded NOT EXECUTED with their exact missing prerequisites, the `READ_CAPACITY` documentation correction, and the open items.
- **[Live panel implementation plan](docs/live-panel-implementation-plan.md)** — the 1 Hz live usage row (requests, prefill/decode TPS, MTP acceptance, MoE GPU hit), its in-memory window contracts, and the 256 MiB → 512 MiB memory-limit change.
- **[Live panel status](docs/live-panel-status.md)** — executed evidence for that plan: commands with exit codes, the live browser smoke transcript and screenshots, fixture payload sizes, and the gates this host could not execute.
- **[Cost estimate implementation plan](docs/cost-estimate-implementation-plan.md)** — the cache-aware API-equivalent estimate: the optional `cached_input` counter, schema version 3, the three rate variables, the priced-hour and coverage rules, and the UI lines they drive.
- **[Cost estimate status](docs/cost-estimate-status.md)** — executed evidence for that plan: commands with exit codes, numeric cost receipts and screenshots.
- **[HUD dashboard implementation plan](docs/hud-dashboard-implementation-plan.md)** — the dark single-palette redesign of the served UI: seven stacked panels, one status strip, the API cost well in the live panel, and the mockup→production mapping with its browser gates.
- **[HUD dashboard status](docs/hud-dashboard-status.md)** — executed evidence for that plan: commands with exit codes, layout and cost-well receipts, the migrated browser smoke transcript and screenshots, and what the mockup alone could not prove.
- **[Deployment](docs/deployment.md)** — build, run, serve, backup, restore and upgrade operations for the self-contained image (collector, API and bundled dashboard; operator-supplied paths, UID/GID and bind address).

The specification and acceptance scenarios remain authoritative. The plans sequence the complete
scope and contain planned verification; the status files carry executed receipts, and
[Delivery 4 status](docs/delivery-4-status.md) names every gate its host could not execute and the
live checks that remain an operator gate.

## Paste into the new repository's agent session

> Implement the Jarvis Token Dashboard described in `docs/spec/specification.md` and `docs/spec/acceptance-scenarios.md`.
>
> Read both documents fully before designing or coding. Treat the selected stack, single-container deployment, 512 MiB memory budget, counter semantics and privacy boundaries as requirements. Use the ordered implementation steps; do not substitute Grafana, VictoriaMetrics, PostgreSQL, a gateway, a Node.js runtime or a multi-service architecture.
>
> Build the complete application: durable collector and SQLite accounting, bounded read-only API, production Vue/Chart.js dashboard, non-root Docker image, Compose example, backup/restore CLI and operations instructions. There is no existing application code to preserve unless this repository already contains user-owned work; inspect first and adapt without deleting it.
>
> Preserve the distinction between observed engine work, request-accounted usage, unavailable data and known zero. Opening balances must not inflate history. All reporting is UTC. Keep exact integer values through storage/API/UI formatting. Make uncertain continuity, gaps, reset boundaries and approximate time attribution visible. Do not silently make the dashboard look complete by inventing zero samples or distributing unknown traffic.
>
> Use behavioral regression tests for the companion scenarios. Run the actual production container against a controlled HTTP fixture, verify the real UI in a browser at desktop and mobile sizes, and execute the documented resource workload. Provide measurements and screenshots, not only a passing unit suite. Do not claim the RAM target or live Jarvis compatibility passed without observing it.
>
> Produce a README with exact build, test, run, backup, restore, upgrade and Unraid deployment commands. Lock dependencies and pin reviewed image versions. Record the tested environment and qualification results in `docs/`. Remove temporary scaffolding; do not leave mock production paths or TODO implementations.
>
> The actual NAS pool path, host bind address/port, UID/GID and live access are operator deployment inputs. Provide configurable examples and finish all local implementation/fixture verification without asking for unnecessary machine details. Do not SSH into Jarvis, restart inference, alter firewall rules or deploy to the NAS without separate operator authorization. If live access is unavailable, explicitly distinguish completed local verification from the pending operator-only gate.

## Expected delivery from the implementing agent

- Working production image and runnable Compose configuration with persistent appdata and explicit private host binding.
- Source, migrations and dependency lockfiles; no proprietary services or external runtime assets.
- Numeric regression evidence, real HTTP/container smoke, browser screenshots and measured memory/latency/database growth.
- Tested online-backup/restore procedure and migration safeguards.
- A concise list of any failed acceptance criteria or operator-only gates, with no fabricated evidence.

## Evidence boundary

Prepared 2026-10-25 from the user's existing Jarvis runbook context, pinned Radiance metric source and official frontend guidance. No live Jarvis or Unraid connection was made. The pinned server exposes the desired counter names in source; the deployed labels, uptime behavior, access and resource fit still need live validation. No Notion integration was available in the authoring session; this packet creates no external task records.
