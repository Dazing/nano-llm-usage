# Jarvis Token Dashboard — portable implementation packet

This folder specifies a new application; it contains no implementation or verified deployment. Copy the whole folder into a new repository as `docs/spec/`. The implementing agent does not need the original HQ repository or its runbook.

## Files and authority

1. **[specification.md](docs/specification.md)** — authoritative product, stack, accounting, data/API/UI, packaging, operations and resource contracts; ordered implementation steps.
2. **[acceptance-scenarios.md](docs/acceptance-scenarios.md)** — normative numeric and failure examples to turn into regression tests and production smoke scenarios.
3. **This README** — context, kickoff prompt and delivery expectations. It does not override the specification.

Selected stack: Python 3.13 + FastAPI/Uvicorn + SQLite; Vue 3 + TypeScript + Vite + Chart.js compiled into static assets. One runtime container, one Python worker, no Node.js runtime. RAM target: 256 MiB hard limit, preferred steady-state process RSS below 100 MiB; both require measurement.

The user chose a DIY application after considering VictoriaMetrics/Grafana because their Unraid NAS is short on RAM. The goal is durable aggregate token history with an attractive simple dashboard, not per-request logs or a general monitoring platform. Earlier RAM estimates were unmeasured and must not become advertised benchmark results.

## Delivery planning

- **[Delivery map](docs/delivery-map.md)** — four cumulative deliveries, acceptance/scenario ownership, dependencies, evidence gates and the separate authorized Jarvis/Unraid gate.
- **[Delivery 1 implementation plan](docs/delivery-1-implementation-plan.md)** — detailed durable collector/accounting contracts, implementation steps and local HTTP/process verification.
- **[Delivery 1 status](docs/delivery-1-status.md)** — implemented collector: executed environment, install/run/test/smoke commands, configuration, storage/locking/migration/retention behavior and numeric smoke receipts.

The specification and acceptance scenarios remain authoritative. These plans sequence the complete scope; they contain planned verification, not executed application or deployment results.

## Paste into the new repository's agent session

> Implement the Jarvis Token Dashboard described in `docs/spec/specification.md` and `docs/spec/acceptance-scenarios.md`.
>
> Read both documents fully before designing or coding. Treat the selected stack, single-container deployment, 256 MiB memory budget, counter semantics and privacy boundaries as requirements. Use the ordered implementation steps; do not substitute Grafana, VictoriaMetrics, PostgreSQL, a gateway, a Node.js runtime or a multi-service architecture.
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
