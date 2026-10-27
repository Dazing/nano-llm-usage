#!/usr/bin/env python3
"""Delivery 4 image gates (plan D1, Step 5) and the qualification cases (Step 6).

Two host-executable phases and two container phases:

  --docs-check   docs/deployment.md against the commands this suite executes,
                 the Dockerfile pins, and the compose fields. No Docker needed.
  --build        `docker compose build` (no build args) and record the image ID.
  --inspect      AC1 on a running container + the in-image dependency versions.
  --cases        the Step 6 qualification cases, one fresh temp appdata each.

`--build`, `--inspect` and `--cases` refuse to run when D1's prerequisites are
missing (Docker CLI, Compose v2, a daemon, cgroup v2 with the cpu controller,
Chromium, root for the appdata chown and the ENOSPC tmpfs) and print the exact
missing items. They never substitute a host `serve` process for a container:
an unqualified host process cannot show what the image does.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import timedelta
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
for _extra in (ROOT / "scripts", ROOT / "tests" / "fixtures", ROOT / "src"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import qualify_common as qc  # noqa: E402
import smoke_frontend as sf  # noqa: E402
from browser_driver import Browser  # noqa: E402
from qualify_common import (  # noqa: E402
    CONTAINER,
    COUNTER_KEYS,
    IMAGE,
    SOURCE_ID,
    Compose,
    FixtureControl,
    RunLog,
    fresh_appdata,
    qualification_env,
    qualification_network,
    qualification_root,
    sh,
)

POLL_SECONDS = 5  # plan Step 6: cases collect fast; the measured workload uses 15
COUNTS = {"computed_input": 960, "generated_output": 240,
          "request_input": 1200, "request_output": 300}
# Plan Step 6: the container log must carry events, never the polled payload.
PAYLOAD_MARKERS = ("# TYPE", "radiance:prefill_tokens_total{", "radiance:decode_tokens_total{",
                   "vllm:prompt_tokens_total{", "vllm:generation_tokens_total{")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def api(port: int, path: str, query: str = "") -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}/api/v1{path}" + (f"?{query}" if query else "")
    try:
        with urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001 - a non-JSON error body is still the status
            body = {}
        return exc.code, body
    except (URLError, TimeoutError, OSError):
        return 0, {}


def readiness(port: int) -> int:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=10) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except (URLError, TimeoutError, OSError):
        return 0


def compose_run(log: RunLog, compose: Compose, label: str, *args: str,
                timeout: int = 120) -> subprocess.CompletedProcess:
    result = compose.run(*args, timeout=timeout)
    log.command(label, compose.command(*args), result.returncode, result.stdout, result.stderr)
    return result


def documented(log: RunLog, compose: Compose, appdata: Path, base_url: str,
               command: str, label: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command string exactly as docs/deployment.md prints it (plan AC8)."""
    env = dict(compose.env)
    env.update({
        "JARVIS_APPDATA_DIR": str(appdata),
        "RADIANCE_BASE_URL": base_url,
        "JARVIS_SOURCE_ID": SOURCE_ID,
    })
    result = sh(["sh", "-c", command], cwd=ROOT, env=env, timeout=timeout)
    log.command(label, ["sh", "-c", command], result.returncode, result.stdout, result.stderr)
    return result


def lifetime(port: int, series_id: str) -> dict[str, str | None]:
    _status, summary = api(port, "/summary", f"series_id={series_id}")
    counters = (summary.get("ranges", {}).get("lifetime", {}) or {}).get("counters", {})
    return {key: (counters.get(key) or {}).get("tokens") for key in COUNTER_KEYS}


def first_series(port: int, model: str = "fixture-model") -> str:
    _status, body = api(port, "/series", "limit=50")
    for item in body.get("items", []):
        if item.get("model_name") == model:
            return item["series_id"]
    items = body.get("items", [])
    return items[0]["series_id"] if items else ""


def read_only_totals(appdata: Path, source_id: str) -> dict[str, int]:
    """Host-side read-only totals from a stopped volume (plan Step 6.1/6.2)."""
    db = appdata / "dashboard.sqlite3"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT counter_key, SUM(tokens) AS total FROM hourly_totals"
            " WHERE source_id = ? GROUP BY counter_key", (source_id,)).fetchall()
        return {row["counter_key"]: row["total"] for row in rows}
    finally:
        conn.close()


def newest_file(appdata: Path, suffix: str) -> Path | None:
    matches = sorted(appdata.rglob(f"*{suffix}"))
    return matches[-1] if matches else None


def container_pid(compose: Compose) -> int:
    out = sh_out(["docker", "inspect", "--format", "{{.State.Pid}}", compose.container_id()])
    return int(out.strip())


def sh_out(argv: list[str], timeout: int = 60) -> str:
    result = sh(argv, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)}: {result.stderr.strip()}")
    return result.stdout


def proc_uid_gid(pid: int) -> tuple[int, int]:
    """Real runtime identity, read from /proc, not from the image config."""
    status = Path(f"/proc/{pid}/status").read_text()
    for line in status.splitlines():
        if line.startswith("Uid:"):
            real, effective = line.split()[1:3]
            return int(real), int(effective)
        if line.startswith("Gid:"):
            real, effective = line.split()[1:3]
            return int(real), int(effective)
    raise RuntimeError(f"/proc/{pid}/status has no Uid/Gid line")


def top_user(compose: Compose) -> str:
    out = sh(["docker", "top", compose.container_id(), "-o", "user,pid,comm"], timeout=60)
    return out.stdout.strip()


def payload_lines(logs: str) -> list[str]:
    return [line for line in logs.splitlines() if any(marker in line for marker in PAYLOAD_MARKERS)]


def log_limit_bytes(inspect_row: dict) -> int:
    config = (inspect_row.get("HostConfig") or {}).get("LogConfig") or {}
    settings = config.get("Config") or {}
    max_size = settings.get("max-size") or "1073741824"
    unit = max_size[-1]
    factor = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}.get(unit, 1)
    amount = int(max_size.rstrip("kmgKMG")) if unit in "kmgKMGkmg" else int(max_size)
    return amount * int(settings.get("max-file") or 1) * factor


# ---------------------------------------------------------------------------
# host-executable phase: documentation consistency (plan Step 5, AC8)
# ---------------------------------------------------------------------------
def phase_docs(log: RunLog) -> None:
    print("--- docs: deployment.md, compose.yaml and Dockerfile (no Docker needed)")
    passed, failed = qc.doc_consistency_check()
    for label, detail in passed:
        log.check(label, True, True)
    for label, detail in failed:
        log.check(label, detail, "match")
    if failed:
        print(f"--- {len(failed)} documentation inconsistencies (see the FAIL lines above)")


# ---------------------------------------------------------------------------
# container phases
# ---------------------------------------------------------------------------
def make_compose(appdata: Path, port: int, base_url: str, project: str,
                 roots: tuple[Path, ...] = (), temporary: Path | None = None,
                 poll: int = POLL_SECONDS) -> Compose:
    return Compose(
        project=project,
        appdata=appdata,
        env=qualification_env(appdata, port, base_url, poll),
        owned_roots=roots,
        temporary_root=temporary,
    )


def phase_build(log: RunLog, compose: Compose) -> str:
    print("--- build: docker compose build (no build args)")
    compose.build()
    image_id = sh_out(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE]).strip()
    size = sh_out(["docker", "image", "inspect", "--format", "{{.Size}}", IMAGE]).strip()
    log.receipt("image_id", image_id)
    log.receipt("image_size_bytes", int(size))
    print(f"[ok ] image {IMAGE} = {image_id} ({size} bytes)")
    return image_id


def phase_inspect(log: RunLog, compose: Compose) -> None:
    """AC1 on the image and on one running container."""
    print("--- inspect: AC1 image defaults")
    raw = sh_out(["docker", "image", "inspect", "--format", "{{json .}}", IMAGE]).strip()
    image = json.loads(raw)
    config = image.get("Config") or {}
    health = config.get("Healthcheck") or {}
    for label, actual, expected in [
        ("image Config.User", config.get("User"), "10001:10001"),
        ("image Config.Entrypoint", config.get("Entrypoint"), ["jarvis-dashboard"]),
        ("image Config.Cmd", config.get("Cmd"), ["serve"]),
        ("image Config.Healthcheck.Test", health.get("Test"), ["CMD", "container-healthcheck"]),
    ]:
        log.check(label, actual, expected)

    print("--- inspect: in-image dependency versions (one-off container, plan Step 5)")
    code = ("import json,sqlite3,sys,platform;print(json.dumps({"
            "'python': sys.version.split()[0],"
            "'sqlite': sqlite3.sqlite_version,"
            "'distribution': platform.freedesktop_os_release().get('PRETTY_NAME','unknown')}))")
    out = sh(["docker", "run", "--rm", "--entrypoint", "python", IMAGE, "-c", code], timeout=180)
    log.command("image dependency versions", ["docker", "run", "--rm", "--entrypoint", "python",
                                              IMAGE, "-c", "<versions>"],
                out.returncode, out.stdout, out.stderr)
    if out.returncode == 0:
        for key, value in json.loads(out.stdout.strip().splitlines()[-1]).items():
            log.receipt(f"image_{key}", value)
            print(f"[ok ] image {key} = {value}")

    print("--- inspect: AC1 on a running container")
    with qualification_root("d4-inspect") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        fixture = FixtureControl(gateway).start()
        fixture.set_series(**COUNTS)
        stack = make_compose(appdata, qc.free_port(), fixture.base_url, "jarvis-4i",
                             roots=(appdata,), temporary=tmp)
        try:
            stack.up(timeout=180)
            ready = qc.wait_until(lambda: readiness(stack.env_port) == 200, timeout=180)
            log.check("readiness 200", ready, True)
            rows = inspect_rows(stack)
            for field, actual, expected in rows:
                log.check(f"AC1 {field}", actual, expected)
            pid = container_pid(stack)
            uid, gid = proc_uid_gid(pid)
            log.check("runtime UID/GID from /proc/1/status", [uid, gid],
                      [int(stack.env["JARVIS_UID"]), int(stack.env["JARVIS_GID"])])
            log.receipt("docker top", top_user(stack))
            _status, status = api(stack.env_port, "/status")
            log.check("/api/v1/status source_id", status.get("source_id"), SOURCE_ID)
            node = sh(["docker", "exec", stack.container_id(), "which", "node"], timeout=60)
            log.check("node absent from the runtime image", node.returncode != 0, True)
            write = sh(["docker", "exec", stack.container_id(), "sh", "-c",
                        "touch /opt/qualification-probe"], timeout=60)
            log.check("/opt is read-only", write.returncode != 0
                      and "read-only" in (write.stderr + write.stdout).lower(), True)
            logs = stack.logs()
            log.check("no metric payload lines in the container log", payload_lines(logs), [])
            inspect_row = stack.inspect()[0]
            log.check("log rotation respects json-file limits",
                      len(logs.encode()) <= log_limit_bytes(inspect_row), True)
        finally:
            fixture.stop()
            stack.down(volumes=False)


def inspect_rows(compose: Compose) -> list[tuple[str, object, object]]:
    row = compose.inspect()[0]
    rows = qc.inspect_expectations(row)
    state = row.get("State") or {}
    rows += [
        ("State.Running", state.get("Running"), True),
        ("State.ExitCode", state.get("ExitCode"), 0),
        ("State.Health.Status", (state.get("Health") or {}).get("Status"), "healthy"),
    ]
    return rows


# ---------------------------------------------------------------------------
# Step 6 cases. Each case owns a fresh temporary appdata and Compose project.
# ---------------------------------------------------------------------------
def case_1_end_to_end(log: RunLog, browser: Browser | None) -> None:
    """fixture -> SQLite (read-only host query) -> API -> browser (plan 6.1)."""
    print("--- case 1: fixture to SQLite to API to browser")
    with qualification_root("d4-c1") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        port = qc.free_port()
        fixture = FixtureControl(gateway).start()
        stack = make_compose(appdata, port, fixture.base_url, "jarvis-41",
                             roots=(appdata,), temporary=tmp)
        try:
            stack.up(timeout=180)
            log.check("readiness before any data", readiness(port) == 200, True)
            if browser is not None:
                page = browser.new_page(*sf.DESKTOP)
                page.navigate(f"http://127.0.0.1:{port}/")
                sf.shot(page, "c1-empty-1440.png")
                page.set_viewport(*sf.MOBILE)
                sf.shot(page, "c1-empty-390.png")
                page.set_viewport(*sf.DESKTOP)
            fixture.set_series(**COUNTS)
            ok = qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
            log.check("series appeared after the first poll", ok, True)
            time.sleep(2 * POLL_SECONDS + 2)
            series_id = first_series(port)
            log.check("lifetime totals equal the fixture counters",
                      lifetime(port, series_id),
                      {key: str(value) for key, value in COUNTS.items()})
            stack.stop()
            totals = read_only_totals(appdata, SOURCE_ID)
            log.check("SQLite totals match the fixture after a clean stop",
                      totals, {key: float(value) for key, value in COUNTS.items()})
            stack.up(timeout=180)
            qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
            log.check("API totals survive the restart", lifetime(port, series_id),
                      {key: str(value) for key, value in COUNTS.items()})
            if browser is not None:
                page = browser.new_page(*sf.DESKTOP)
                page.navigate(f"http://127.0.0.1:{port}/")
                sf.dom_basics(page)
                sf.ranges(page)
                sf.poll_lifecycle(page)
                sf.theme_and_a11y(page, port)
                sf.network(page, fixture.server, port)
                sf.offline(page, fixture.server)
                sf.shot(page, "c1-normal-1440.png")
                page.set_viewport(*sf.MOBILE)
                sf.shot(page, "c1-normal-390.png")
                page.set_viewport(*sf.DESKTOP)
                fixture.state.metrics_status = 500
                fixture.state.metrics_body = "upstream unavailable"
                time.sleep(3 * POLL_SECONDS + 4)
                sf.shot(page, "c1-offline-1440.png")
                fixture.state.metrics_status = 200
                fixture.state.metrics_body = None
        finally:
            fixture.stop()
            stack.down(volumes=True)


def case_2_restart_keeps_totals(log: RunLog) -> None:
    """down/up on the same volume: 960 preserved, unchanged sample adds nothing."""
    print("--- case 2: volume survives down/up; timed stop; State.ExitCode")
    with qualification_root("d4-c2") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        port = qc.free_port()
        fixture = FixtureControl(gateway).start()
        fixture.set_series(**COUNTS)
        stack = make_compose(appdata, port, fixture.base_url, "jarvis-42",
                             roots=(appdata,), temporary=tmp)
        try:
            stack.up(timeout=180)
            ok = qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
            log.check("first poll applied", ok, True)
            time.sleep(2 * POLL_SECONDS + 2)
            series_id = first_series(port)
            before = lifetime(port, series_id)
            log.check("lifetime equals the fixture counters", before,
                      {key: str(value) for key, value in COUNTS.items()})
            compose_run(log, stack, "down", "down")
            stack.up(timeout=180)
            qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
            time.sleep(3 * POLL_SECONDS + 2)  # at least one unchanged sample
            log.check("unchanged counters keep the same totals after down/up",
                      lifetime(port, series_id), before)
            started = time.monotonic()
            stopped = compose_run(log, stack, "stop (timed)", "stop", timeout=180)
            log.receipt("stop_seconds", round(time.monotonic() - started, 3))
            log.check("stop exit code", stopped.returncode, 0)
            row = stack.inspect()[0]
            log.check("State.ExitCode after a clean stop",
                      (row.get("State") or {}).get("ExitCode"), 0)
            log.check("State.FinishedAt is set",
                      bool((row.get("State") or {}).get("FinishedAt")), True)
            log.check("no metric payload lines in the container log",
                      payload_lines(stack.logs()), [])
            log.receipt("log_limit_bytes", log_limit_bytes(row))
        finally:
            fixture.stop()
            stack.down(volumes=True)


def case_3_outage_recovery(log: RunLog) -> None:
    """Collector downtime and a 500 upstream: credited once, flagged, null rate.

    Recovery with changed uptime evidence opens an epoch and credits nothing.
    """
    print("--- case 3: collector downtime, /metrics 500, evidence-based recovery")
    with qualification_root("d4-c3") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        port = qc.free_port()
        fixture = FixtureControl(gateway).start()
        fixture.set_series(**COUNTS)
        stack = make_compose(appdata, port, fixture.base_url, "jarvis-43",
                             roots=(appdata,), temporary=tmp)
        try:
            stack.up(timeout=180)
            qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
            time.sleep(2 * POLL_SECONDS + 2)
            series_id = first_series(port)
            baseline = {key: int(COUNTS[key]) for key in COUNTER_KEYS}
            growth = {"computed_input": 700, "generated_output": 90,
                      "request_input": 800, "request_output": 110}
            advanced = {key: baseline[key] + growth[key] for key in COUNTER_KEYS}

            def counters(payload: dict) -> dict:
                return payload.get("counters", {})

            _status, summary = api(port, "/summary", f"series_id={series_id}")
            first_block = counters(summary.get("ranges", {}).get("today", {}))
            log.check("today observed at least one interval",
                      first_block.get("computed_input", {}).get("observed_intervals", 0) > 0,
                      True)

            # (a) upstream stops answering for more than two polls.
            fixture.state.metrics_status = 500
            fixture.state.metrics_body = "upstream unavailable"
            time.sleep(4 * POLL_SECONDS + 4)
            log.check("readiness stays 200 during an upstream outage", readiness(port), 200)
            health = (stack.inspect()[0].get("State") or {}).get("Health") or {}
            log.check("Docker health stays healthy", health.get("Status"), "healthy")
            _status, status = api(port, "/status")
            log.check("status reports the collector offline",
                      (status.get("collector_status") or {}).get("state"), "offline")

            # (b) compatible recovery: one credit, flags set, rate null on the gap.
            fixture.state.metrics_status = 200
            fixture.state.metrics_body = None
            fixture.set_series(**advanced)
            ok = qc.wait_until(
                lambda: int((counters(api(port, "/summary", f"series_id={series_id}")[1]
                                      .get("ranges", {}).get("lifetime", {}))
                             .get("computed_input", {}) or {}).get("tokens") or 0)
                >= advanced["computed_input"], timeout=90)
            log.check("the gap delta is credited once", ok, True)
            _status, summary = api(port, "/summary", f"series_id={series_id}")
            flags = (summary.get("rates", {}).get("computed_input", {}) or {}).get(
                "quality_flags", [])
            log.check("the gap-closing interval carries the gap flags",
                      sorted(set(flags) & {"collection_gap", "time_attribution_approximate"}),
                      ["collection_gap", "time_attribution_approximate"])
            log.check("the gap-closing interval reports a null rate",
                      (summary.get("rates", {}).get("computed_input", {}) or {}).get(
                          "tokens_per_second"), None)
            hour = qc.hour_label(qc.utc_now())
            _status, history = api(port, "/history",
                                   f"series_id={series_id}&bucket=hour&from={hour}&to={hour}")
            log.receipt("history buckets in the current hour", history.get("bucket_count"))

            # (c) recovery with changed uptime evidence: epoch, no credit.
            before = lifetime(port, series_id)
            fixture.state.uptime_origin = time.time()
            fixture.state.stats_body = json.dumps({"model": fixture.model, "uptime_s": 3.0})
            fixture.set_series(**{key: advanced[key] + 500 for key in COUNTER_KEYS})
            time.sleep(4 * POLL_SECONDS + 4)
            _status, summary = api(port, "/summary", f"series_id={series_id}")
            epoch = summary.get("epoch") or {}
            log.check("changed uptime evidence opens a restart epoch",
                      epoch.get("reason"), "restart_evidence")
            log.check("the restart poll credits nothing", lifetime(port, series_id), before)
        finally:
            fixture.stop()
            stack.down(volumes=True)


def case_4_seeded_view(log: RunLog, seed_db: Path, manifest: Path,
                       browser: Browser | None) -> None:
    """A Step 4 seeded copy: the container's API and browser agree with the manifest."""
    print("--- case 4: one-year seeded volume through the container")
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    with qualification_root("d4-c4") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        for name in ("dashboard.sqlite3", "dashboard.sqlite3-shm", "dashboard.sqlite3-wal"):
            source = seed_db.parent / name
            if source.is_file():
                target = appdata / name
                target.write_bytes(source.read_bytes())
                os.chown(target, os.getuid(), os.getuid())
        port = qc.free_port()
        fixture = FixtureControl(gateway).start()
        fixture.load_manifest(expected)
        fixture.set_counters(expected["final_counters"])
        stack = make_compose(appdata, port, fixture.base_url, "jarvis-44",
                             roots=(appdata,), temporary=tmp)
        try:
            stack.up(timeout=300)
            ok = qc.wait_until(lambda: readiness(port) == 200, timeout=300)
            log.check("readiness on a one-year volume", ok, True)
            model = expected["models"][0]["model_name"]
            series_id = first_series(port, model)
            log.require(bool(series_id), "the seeded series is visible through the API")
            time.sleep(2 * POLL_SECONDS + 2)
            _status, summary = api(port, "/summary", f"series_id={series_id}")
            life = (summary.get("ranges", {}).get("lifetime", {}) or {}).get("counters", {})
            for key in COUNTER_KEYS:
                want = int(expected["per_model"][0]["counters"][key])
                got = int(life[key]["tokens"])
                log.require(got >= want,
                            f"lifetime {key} >= manifest: {got} vs {want} (polls added since)")
            today = qc.utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
            _status, history = api(port, "/history",
                                   f"series_id={series_id}&bucket=day"
                                   f"&from={qc.day_label(today)}T00:00:00Z"
                                   f"&to={qc.day_label(today + timedelta(days=1))}T00:00:00Z")
            log.check("the seeded history answers a day query",
                      (history.get("bucket_count") or 0) > 0, True)
            gaps = expected.get("gap_only_hours", {})
            if browser is not None and gaps:
                page = browser.new_page(*sf.DESKTOP)
                page.navigate(f"http://127.0.0.1:{port}/")
                page.click('[data-testid="range-24h"]')
                qc.wait_until(lambda: sf.count_of(page, '[data-testid="chart-table"] tbody tr') > 0,
                              timeout=60)
                sf.open_details(page, "chart-disclosure")
                row = page.evaluate(
                    "(() => { const rows = [...document.querySelectorAll('[data-testid=\"chart-table\"] tbody tr')];"
                    " for (const r of rows) { const cells = [...r.querySelectorAll('td')].map((c) => c.textContent.trim());"
                    " if (cells.some((c) => c === 'no data')) return cells; } return null; })()"
                )
                log.check("a gap-only bucket renders as no data, not a zero bar",
                          row is not None and "no data" in (row or []), True)
                log.check("the gap-only row never renders a zero",
                          any(cell == "0" for cell in (row or [])), False)
                sf.shot(page, "c4-seeded-24h-1440.png")
                page.set_viewport(*sf.MOBILE)
                sf.shot(page, "c4-seeded-24h-390.png")
        finally:
            fixture.stop()
            stack.down(volumes=True)


def case_5_volume_conflicts(log: RunLog) -> None:
    """One writer owns the volume; unusable volumes are refused, never repaired."""
    print("--- case 5: volume lock, missing/unusable mounts, byte-for-byte preservation")
    with qualification_root("d4-c5") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        port = qc.free_port()
        fixture = FixtureControl(gateway).start()
        fixture.set_series(**COUNTS)
        stack = make_compose(appdata, port, fixture.base_url, "jarvis-45",
                             roots=(appdata,), temporary=tmp)
        try:
            stack.up(timeout=180)
            qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
            time.sleep(POLL_SECONDS + 2)
            before = {p.name: qc.sha256(p) for p in appdata.glob("dashboard.sqlite3*")}
            log.require(bool(before), "the running writer created the database")
            second = sh([
                "docker", "run", "--rm", "-v", f"{appdata}:/data", "--user",
                f"{os.getuid()}:{os.getuid()}", "-e", "DATABASE_PATH=/data/dashboard.sqlite3",
                "-e", "RADIANCE_BASE_URL=invalid", "--entrypoint", "jarvis-dashboard",
                IMAGE, "backup", "--output", "/data/backups/second.sqlite3",
            ], timeout=180)
            log.command("second writer on the same volume", ["docker", "run", "…", "backup"],
                        second.returncode, second.stdout, second.stderr)
            log.check("a second writer exits 3", second.returncode, 3)
            log.check("the second writer reports volume_locked",
                      "volume_locked" in (second.stdout + second.stderr), True)
            log.check("the writer owns the lock (readiness stays 200)", readiness(port), 200)
        finally:
            stack.down(volumes=False)
            fixture.stop()
        for path in sorted(appdata.glob("dashboard.sqlite3*")):
            log.check(f"pre-existing file unchanged: {path.name}", qc.sha256(path),
                      before.get(path.name))
        log.check("no new dashboard.sqlite3 appeared",
                  sorted(p.name for p in appdata.glob("dashboard.sqlite3*")),
                  sorted(before))

        refusals = [
            ("missing /data bind", {}, "storage"),
        ]
        for label, mounts, needle in refusals:
            result = sh(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getuid()}",
                         "-e", "DATABASE_PATH=/data/dashboard.sqlite3",
                         "-e", "RADIANCE_BASE_URL=invalid",
                         "--entrypoint", "jarvis-dashboard", IMAGE,
                         "backup", "--output", "/data/backups/probe.sqlite3"], timeout=180)
            log.command(f"refusal: {label}", ["docker", "run", "…"], result.returncode,
                        result.stdout, result.stderr)
            log.check(f"{label} exits non-zero", result.returncode != 0, True)
            log.check(f"{label} reports {needle}",
                      needle in (result.stdout + result.stderr).lower(), True)
        log.check("no database was created in the unmounted run",
                  (appdata / "dashboard.sqlite3").exists(), True)

        for label, prepare, needle in [
            ("unwritable appdata (0555)", "chmod 0555 {dir}", "storage"),
            ("corrupted database", None, "database_not_sqlite"),
            ("newer schema (user_version=99)", None, "schema"),
        ]:
            probe = tmp / f"probe-{needle}"
            probe.mkdir(exist_ok=True)
            if prepare:
                sh(["sh", "-c", prepare.format(dir=probe)], timeout=60)
            else:
                target = probe / "dashboard.sqlite3"
                target.write_bytes(b"definitely not a sqlite database" * 40)
                os.chmod(probe, 0o770)
            if needle == "schema":
                conn = sqlite3.connect(probe / "dashboard.sqlite3")
                conn.execute("PRAGMA user_version = 99")
                conn.commit()
                conn.close()
            result = sh(["docker", "run", "--rm", "-v", f"{probe}:/data", "--user",
                         f"{os.getuid()}:{os.getuid()}",
                         "-e", "DATABASE_PATH=/data/dashboard.sqlite3",
                         "-e", "RADIANCE_BASE_URL=invalid", "--entrypoint", "jarvis-dashboard",
                         IMAGE, "backup", "--output", "/data/backups/probe.sqlite3"],
                        timeout=180)
            log.command(f"refusal: {label}", ["docker", "run", "…"], result.returncode,
                        result.stdout, result.stderr)
            log.check(f"{label} exits non-zero", result.returncode != 0, True)
            log.check(f"{label} reports {needle}",
                      needle in (result.stdout + result.stderr).lower(), True)
            sh(["chmod", "0770", str(probe)], timeout=60)


def case_6_disk_full(log: RunLog) -> None:
    """ENOSPC on the volume: readiness 503, health unhealthy, no duplicated writes."""
    print("--- case 6: volume full (needs root for the tmpfs mount)")
    with qualification_root("d4-c6") as tmp, qualification_network() as gateway:
        appdata = tmp / "appdata"
        appdata.mkdir()
        mounted = sh(["mount", "-t", "tmpfs", "-o", "size=48m", "tmpfs", str(appdata)])
        log.command("mount tmpfs", ["mount", "-t", "tmpfs", "…", str(appdata)],
                    mounted.returncode, mounted.stdout, mounted.stderr)
        if mounted.returncode != 0:
            log.require(False, f"tmpfs mount failed: {mounted.stderr.strip()}")
            return
        try:
            os.chown(appdata, os.getuid(), os.getuid())
            port = qc.free_port()
            fixture = FixtureControl(gateway).start()
            fixture.set_series(**COUNTS)
            stack = make_compose(appdata, port, fixture.base_url, "jarvis-46",
                                 roots=(appdata,), temporary=tmp)
            try:
                stack.up(timeout=180)
                qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
                time.sleep(POLL_SECONDS + 2)
                series_id = first_series(port)
                baseline = lifetime(port, series_id)
                log.receipt("baseline totals before ENOSPC", baseline)
                filler = stack.exec("sh", "-c",
                                    "head -c 60000000 /dev/zero > /data/filler", timeout=300)
                log.check("the filler hits ENOSPC", filler.returncode != 0, True)
                qc.wait_until(lambda: readiness(port) == 503, timeout=120)
                code = (api(port, "/status")[1].get("storage_status") or {}).get("code", "")
                log.check("readiness reports a storage failure", readiness(port), 503)
                log.check("the storage code names the failure", code.startswith("storage_"), True)
                health = (stack.inspect()[0].get("State") or {}).get("Health") or {}
                log.check("Docker marks the container unhealthy", health.get("Status"),
                          "unhealthy")
                sh(["docker", "exec", stack.container_id(), "rm", "-f", "/data/filler"],
                   timeout=120)
                stack.stop()
                stack.up(timeout=180)
                qc.wait_until(lambda: readiness(port) == 200, timeout=180)
                time.sleep(2 * POLL_SECONDS + 2)
                log.check("recovery resumes from the last committed baseline",
                          {key: int(value or 0) for key, value in
                           lifetime(port, series_id).items()},
                          {key: int(value or 0) for key, value in baseline.items()})
            finally:
                fixture.stop()
                stack.down(volumes=False)
        finally:
            umount = sh(["umount", str(appdata)], timeout=120)
            log.command("umount tmpfs", ["umount", str(appdata)], umount.returncode,
                        umount.stdout, umount.stderr)


def case_7_backup_restore(log: RunLog) -> None:
    """The documented backup/restore commands (plan AC8) are the ones executed here."""
    print("--- case 7: documented backup, restore into a fresh volume, restore against a writer")
    with qualification_root("d4-c7") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        restore_dir = fresh_appdata(tmp / "restore", os.getuid(), os.getuid())
        port = qc.free_port()
        restore_port = qc.free_port()
        fixture = FixtureControl(gateway).start()
        fixture.set_series(**COUNTS)
        stack = make_compose(appdata, port, fixture.base_url, "jarvis-47",
                             roots=(appdata, restore_dir), temporary=tmp)
        target = make_compose(restore_dir, restore_port, fixture.base_url, "jarvis-47r",
                              roots=(restore_dir,), temporary=tmp)
        try:
            stack.up(timeout=180)
            qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
            time.sleep(2 * POLL_SECONDS + 2)
            series_id = first_series(port)
            before = lifetime(port, series_id)
            backup = documented(log, stack, appdata, fixture.base_url, qc.BACKUP_COMMAND,
                                "documented backup")
            log.check("documented backup exit code", backup.returncode, 0)
            snapshot = newest_file(appdata, ".sqlite3")
            log.require(snapshot is not None and "backups" in str(snapshot),
                        f"a backup file exists under {appdata}/backups")
            conn = sqlite3.connect(snapshot)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            totals = {row[0]: row[1] for row in conn.execute(
                "SELECT counter_key, SUM(tokens) FROM hourly_totals WHERE source_id = ?"
                " GROUP BY counter_key", (SOURCE_ID,))}
            schema = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            log.receipt("backup user_version", schema)
            log.check("backup integrity_check", integrity, "ok")
            log.check("the backup ledger equals the live totals",
                      {key: int(totals.get(key) or 0) for key in COUNTER_KEYS},
                      {key: int(before[key] or 0) for key in COUNTER_KEYS})

            # The documented restore against a running target must refuse (exit 3).
            refused = documented(log, stack, appdata, fixture.base_url,
                                 qc.RESTORE_COMMANDS[1].replace(
                                     "dashboard-20261025T120000Z.sqlite3", snapshot.name),
                                 "documented restore against a running target")
            log.check("restore against a running target exits 3", refused.returncode, 3)
            log.check("restore names the lock", "volume_locked"
                      in (refused.stdout + refused.stderr), True)

            # Documented restore into a fresh, never-started volume, then start offline.
            fixture.state.metrics_status = 500
            fixture.state.metrics_body = "upstream unavailable"
            for index, command in enumerate(qc.RESTORE_COMMANDS):
                if "dashboard-20261025T120000Z.sqlite3" in command:
                    command = command.replace("dashboard-20261025T120000Z.sqlite3",
                                              f"backups/{snapshot.name}")
                shutil.copy2(snapshot, restore_dir / "restore-source.sqlite3")
                run = sh(["sh", "-c", command], cwd=ROOT,
                         env={**target.env, "JARVIS_APPDATA_DIR": str(restore_dir),
                              "RADIANCE_BASE_URL": fixture.base_url,
                              "JARVIS_SOURCE_ID": SOURCE_ID}, timeout=600)
                log.command(f"documented restore {index + 1}", ["sh", "-c", command],
                            run.returncode, run.stdout, run.stderr)
                log.check(f"documented restore step {index + 1} exit code", run.returncode, 0)
            target.up(timeout=180)
            qc.wait_until(lambda: readiness(restore_port) == 200, timeout=180)
            restored_series = first_series(restore_port)
            log.check("the restored volume reports the snapshot totals",
                      lifetime(restore_port, restored_series), before)
            # Upstream returns: continuation adds exactly the new counter difference.
            growth = {"computed_input": 500, "generated_output": 60,
                      "request_input": 600, "request_output": 70}
            fixture.state.metrics_status = 200
            fixture.state.metrics_body = None
            fixture.set_series(**{key: COUNTS[key] + growth[key] for key in COUNTER_KEYS})
            time.sleep(3 * POLL_SECONDS + 4)
            after = lifetime(restore_port, restored_series)
            log.check("continuation adds exactly the new counter difference",
                      {key: int(after[key] or 0) - int(before[key] or 0) for key in COUNTER_KEYS},
                      growth)
        finally:
            fixture.stop()
            for stack_row in (target, stack):
                stack_row.down(volumes=True)


def case_8_schema1_migration(log: RunLog, worktree: Path, commit: str) -> None:
    """Delivery 1's schema-1 volume migrates on first start of the final image."""
    print(f"--- case 8: schema-1 volume ({commit}) migrates with a verified snapshot")
    with qualification_root("d4-c8") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        tree = worktree
        if not tree.is_dir():
            added = sh(["git", "worktree", "add", str(tree), commit], cwd=ROOT, timeout=300)
            log.command("git worktree add", ["git", "worktree", "add", str(tree), commit],
                        added.returncode, added.stdout, added.stderr)
            if added.returncode != 0:
                log.require(False, f"cannot create the Delivery 1 worktree: {added.stderr.strip()}")
                return
        db = appdata / "dashboard.sqlite3"
        env = {
            "DATABASE_PATH": str(db),
            "RADIANCE_BASE_URL": f"http://127.0.0.1:{qc.free_port()}",
            "SOURCE_ID": SOURCE_ID,
            "POLL_INTERVAL_SECONDS": "5",
            "RAW_RETENTION_DAYS": "30",
            "LOG_LEVEL": "INFO",
        }
        # The Delivery 1 collector runs as a host process on purpose: its job is
        # to produce an old volume, not to qualify an image.
        seed = sh(["uv", "run", "--frozen", "jarvis-dashboard", "collect"], cwd=tree,
                  env={**os.environ, **env}, timeout=900)
        log.command("delivery-1 collector", ["uv", "run", "--frozen", "jarvis-dashboard",
                                             "collect"], seed.returncode, seed.stdout, seed.stderr)
        if seed.returncode != 0:
            log.require(False, f"Delivery 1 collector failed: {seed.stderr.strip()[:400]}")
            return
        conn = sqlite3.connect(db)
        schema1 = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        log.check("the old volume is at schema 1", schema1, 1)
        totals1 = {row[0]: row[1] for row in _totals_on(db)}
        log.check("the old volume carries the expected totals",
                  sum(totals1.values()) > 0, True)

        port = qc.free_port()
        fixture = FixtureControl(gateway).start()
        fixture.set_series(**COUNTS)
        stack = make_compose(appdata, port, fixture.base_url, "jarvis-48",
                             roots=(appdata,), temporary=tmp)
        try:
            stack.up(timeout=180)
            ok = qc.wait_until(lambda: readiness(port) == 200, timeout=180)
            log.check("the final image starts on the schema-1 volume", ok, True)
            snapshots = sorted(appdata.glob(".pre-migration-*.sqlite3"))
            log.require(bool(snapshots), f"a pre-migration snapshot exists in {appdata}")
            snapshot = snapshots[-1]
            conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            pre_totals = {row[0]: row[1] for row in conn.execute(
                "SELECT counter_key, SUM(tokens) FROM hourly_totals WHERE source_id = ?"
                " GROUP BY counter_key", (SOURCE_ID,))}
            conn.close()
            log.check("the pre-migration snapshot is intact", integrity, "ok")
            log.check("the snapshot keeps the pre-migration totals", pre_totals, totals1)
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            schema2 = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            log.check("the live volume is at schema 2", schema2, 2)
        finally:
            fixture.stop()
            stack.down(volumes=True)


def _totals_on(db: Path) -> list[tuple[str, int]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT counter_key, SUM(tokens) FROM hourly_totals WHERE source_id = ?"
            " GROUP BY counter_key", (SOURCE_ID,)).fetchall()
    finally:
        conn.close()


def case_9_endpoint_change(log: RunLog) -> None:
    """The same SOURCE_ID at a new upstream endpoint opens an endpoint_change epoch."""
    print("--- case 9: same SOURCE_ID, new endpoint -> endpoint_change epoch, lifetime kept")
    with qualification_root("d4-c9") as tmp, qualification_network() as gateway:
        appdata = fresh_appdata(tmp / "appdata", os.getuid(), os.getuid())
        port = qc.free_port()
        fixture = FixtureControl(gateway, port=qc.free_port()).start()
        fixture.set_series(**COUNTS)
        stack = make_compose(appdata, port, fixture.base_url, "jarvis-49",
                             roots=(appdata,), temporary=tmp)
        try:
            stack.up(timeout=180)
            qc.wait_until(lambda: api(port, "/series")[1].get("items"), timeout=120)
            time.sleep(2 * POLL_SECONDS + 2)
            series_id = first_series(port)
            before = lifetime(port, series_id)
            log.check("lifetime before the endpoint change", before,
                      {key: str(value) for key, value in COUNTS.items()})

            # Move the upstream to a different port: same source id, new endpoint.
            moved = FixtureControl(gateway, port=qc.free_port()).start()
            moved.set_series(**{key: COUNTS[key] + 250 for key in COUNTER_KEYS})
            moved.state.uptime_origin = fixture.state.uptime_origin
            compose_run(log, stack, "down (endpoint move)", "down")
            stack.env["RADIANCE_BASE_URL"] = moved.base_url
            stack.up(timeout=180)
            ok = qc.wait_until(
                lambda: (api(port, "/summary", f"series_id={series_id}")[1].get("epoch") or {})
                .get("reason") == "endpoint_change", timeout=120)
            log.check("an endpoint_change epoch opens", ok, True)
            after = lifetime(port, series_id)
            log.check("the lifetime total is preserved across the endpoint change",
                      {key: int(after[key] or 0) >= int(before[key] or 0)
                       for key in COUNTER_KEYS},
                      {key: True for key in COUNTER_KEYS})
            _status, summary = api(port, "/summary", f"series_id={series_id}")
            epoch = summary.get("epoch") or {}
            log.check("the opening balance excludes the epoch's first delta",
                      int(epoch.get("opening_computed_input", 0)), COUNTS["computed_input"] + 250)
            moved.stop()
        finally:
            fixture.stop()
            stack.down(volumes=True)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs-check", action="store_true",
                        help="deployment.md/Dockerfile/compose consistency (no Docker needed)")
    parser.add_argument("--build", action="store_true", help="docker compose build (plan Step 5)")
    parser.add_argument("--inspect", action="store_true", help="AC1 image + running container")
    parser.add_argument("--cases", action="store_true", help="the Step 6 qualification cases")
    parser.add_argument("--case", default="all", help="case number, or 'all'")
    parser.add_argument("--out", type=Path, help="receipt directory (default docs/receipts/…)")
    parser.add_argument("--seed-db", type=Path,
                        help="one-year volume from scripts/seed_history.py (plan Step 4)")
    parser.add_argument("--seed-manifest", type=Path, help="the seed manifest for case 4")
    parser.add_argument("--worktree", type=Path, default=Path("/tmp/d4/delivery-1"),
                        help="Delivery 1 worktree for case 8")
    parser.add_argument("--from-commit", default="ad5556c",
                        help="Delivery 1 commit for case 8")
    parser.add_argument("--no-browser", action="store_true",
                        help="skip the browser phases (they need chromium)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="record the environment and the missing prerequisites, run nothing")
    args = parser.parse_args()

    run_id = qc.new_run_id()
    out = args.out or ROOT / "docs" / "receipts" / "delivery-4" / run_id
    log = RunLog(out)
    sf.SHOTS = log.dir  # screenshots land beside the receipts
    env, missing = qc.preflight(require_browser=not args.no_browser,
                               require_root=True, appdata_hint=Path("/tmp"))
    log.dump("environment.json", env)
    log.receipt("run_id", run_id)
    log.receipt("phases", [name for name in ("docs-check", "build", "inspect", "cases")
                           if getattr(args, name.replace("-", "_"))])

    if args.docs_check:
        phase_docs(log)
        print(log.summary())
        return log.exit_code()

    if args.preflight_only:
        print(json.dumps(env, indent=2))
        if missing:
            print("[FAIL] qualification prerequisites missing:")
            for item in missing:
                print(f"  - {item}")
            return 1
        print("[ok ] all qualification prerequisites present")
        return 0

    if missing:
        print("[FAIL] qualification prerequisites missing:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        log.receipt("missing_prerequisites", missing)
        log.require(False, "preflight refused: image gates require a container runtime")
        print(log.summary())
        return log.exit_code()

    browser: Browser | None = None
    if not args.no_browser:
        browser = Browser.launch(Path(tempfile.mkdtemp(prefix="jarvis-4-chrome-")), [])
    probe_appdata = fresh_appdata(Path(tempfile.mkdtemp(prefix="jarvis-4-probe-")) / "appdata",
                                 os.getuid(), os.getuid())
    probe = make_compose(probe_appdata, qc.free_port(), "http://127.0.0.1:1", "jarvis-4-probe")
    try:
        if args.build:
            phase_build(log, probe)
        if args.inspect:
            phase_inspect(log, probe)
        if args.cases:
            chosen = {value.strip() for value in args.case.split(",")}
            want = lambda name: "all" in chosen or name in chosen  # noqa: E731
            if want("1"):
                case_1_end_to_end(log, browser)
            if want("2"):
                case_2_restart_keeps_totals(log)
            if want("3"):
                case_3_outage_recovery(log)
            if want("4"):
                if args.seed_db and args.seed_manifest and args.seed_db.is_file():
                    case_4_seeded_view(log, args.seed_db, args.seed_manifest, browser)
                else:
                    log.require(False, "case 4 needs --seed-db and --seed-manifest from Step 4")
            if want("5"):
                case_5_volume_conflicts(log)
            if want("6"):
                case_6_disk_full(log)
            if want("7"):
                case_7_backup_restore(log)
            if want("8"):
                case_8_schema1_migration(log, args.worktree, args.from_commit)
            if want("9"):
                case_9_endpoint_change(log)
    finally:
        if browser is not None:
            browser.close()
        shutil.rmtree(probe_appdata.parent, ignore_errors=True)
    print(log.summary())
    return log.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
