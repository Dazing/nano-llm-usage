#!/usr/bin/env python3
"""Shared local-qualification harness (docs/delivery-4-implementation-plan.md).

Preflight, private network, Compose/inspect wrappers, host-side samplers and
receipt writers shared by `scripts/qualify_image.py` (build, inspection,
end-to-end cases) and `scripts/qualify_resources.py` (spec D9 workload).

Contract (plan D1/D2/D3/D6/D8):
- Every measured step runs against a container built from the locked inputs.
  A host `serve` process never substitutes for an image gate.
- The preflight records the host/runtime facts and refuses to start measurement
  unless the container runtime contract is met, naming exactly what is missing.
- The production `compose.yaml` runs unchanged; the only addition is
  `tests/fixtures/compose.qualification.yaml` (network attachment).
- Sampling is host-side and read-only, so samplers add no processes to the
  measured cgroup.

Usage:
  .venv/bin/python scripts/qualify_common.py --preflight [--out DIR] [--browser]
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from fixture_server import FixtureServer, install_fixture  # noqa: E402
from jarvis_dashboard.domain import COUNTER_KEYS, hour_label, to_iso  # noqa: E402

# --- D1 runtime contract ---------------------------------------------------
MEMORY_MAX = "536870912"  # 512 MiB, spec D8 as raised by the live panel plan (D8)
CPU_MAX = "50000 100000"  # 0.5 CPU
PIDS_MAX = "128"
CONTAINER = "dashboard"
IMAGE = "jarvis-dashboard:0.1.0"

# --- D3 private fixture network --------------------------------------------
NETWORK = "jarvis-qual"
NETWORK_SUBNET = "172.28.201.0/24"
NETWORK_GATEWAY = "172.28.201.1"

# --- D2 qualification source identity --------------------------------------
SOURCE_ID = "qual-jarvis"

LOOPBACK = "127.0.0.1"
DOCKERFILE = ROOT / "Dockerfile"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"

# Digest-pinned build inputs (AC1: the image is reproducible from locked inputs).
PINNED_BASE_IMAGES = (
    "node:24.22.1@sha256:6449e19d",
    "ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39",
    "python:3.13.17-slim@sha256:d98cc29d",
)

# Pinned bases whose rootfs has no shell: uv's plain tag carries /uv and /uvx
# only, so runc cannot exec /bin/sh for a shell-form RUN in a stage built FROM it
# (`exec: /bin/sh: stat /bin/sh: no such file or directory`). Such an image is a
# binary source for COPY --from and nothing more.
SHELL_LESS_BASES = ("ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39",)

COMPOSE_FILES = ["compose.yaml", "tests/fixtures/compose.qualification.yaml"]
LOCKFILES = {
    "uv_lock": ROOT / "uv.lock",
    "package_lock": ROOT / "frontend" / "package-lock.json",
}


# ---------------------------------------------------------------------------
# output convention (same shape as scripts/smoke.py)
# ---------------------------------------------------------------------------
def receipt(name: str, value: object) -> None:
    print(f"[receipt] {name} = {value}", flush=True)


class Verifier:
    """Collects check outcomes so one case run reports every failure."""

    def __init__(self) -> None:
        self.results: list[tuple[bool, str, object, object]] = []

    def check(self, name: str, actual: object, expected: object) -> bool:
        ok = actual == expected
        self.results.append((ok, name, actual, expected))
        print(
            f"[{'ok ' if ok else 'FAIL'}] {name}: {actual!r} (expected {expected!r})",
            flush=True,
        )
        return ok

    def receipt(self, name: str, value: object) -> None:
        receipt(name, value)

    @property
    def failures(self) -> list[tuple[str, object, object]]:
        return [(name, actual, expected) for ok, name, actual, expected in self.results if not ok]

    def require(self, condition: bool, message: str) -> bool:
        return self.check(message, bool(condition), True)

    def exit_code(self) -> int:
        failed = len(self.failures)
        print(f"[receipt] checks={len(self.results)} failures={failed}", flush=True)
        return 1 if failed else 0


# ---------------------------------------------------------------------------
# receipts (plan D8)
# ---------------------------------------------------------------------------
class Receipts:
    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def jsonl(self, name: str, record: dict) -> None:
        with (self.dir / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def dump(self, name: str, payload: object) -> Path:
        return self.write(name, json.dumps(payload, indent=2, sort_keys=True) + "\n")


class RunLog(Receipts):
    """Receipts plus a Verifier: prints, collects and persists every step.

    The image and resource phases run for a long time and must report *all*
    failures of a run, not the first one, so checks are collected; every external
    command is appended to `commands.jsonl` so a receipt line is always traceable
    to the exact invocation that produced it (plan D8).
    """

    def __init__(self, run_dir: Path) -> None:
        super().__init__(run_dir)
        self.verifier = Verifier()

    def check(self, name: str, actual: object, expected: object) -> bool:
        return self.verifier.check(name, actual, expected)

    def require(self, condition: bool, message: str) -> bool:
        return self.verifier.require(condition, message)

    def receipt(self, name: str, value: object) -> None:
        self.verifier.receipt(name, value)

    def note(self, name: str, value: object) -> None:
        receipt(name, value)

    def command(self, label: str, argv: list[str], code: int, stdout: str = "",
                stderr: str = "", seconds: float = 0.0) -> None:
        self.jsonl("commands.jsonl", {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "label": label,
            "argv": argv,
            "exit_code": code,
            "seconds": round(seconds, 3),
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        })

    @property
    def failures(self) -> list[tuple[str, object, object]]:
        return self.verifier.failures

    def exit_code(self) -> int:
        return self.verifier.exit_code()

    def summary(self) -> str:
        checks = self.verifier.results
        failed = self.failures
        self.dump("checks.json", [
            {"name": name, "ok": ok, "actual": actual, "expected": expected}
            for ok, name, actual, expected in checks
        ])
        text = f"[receipt] checks={len(checks)} failures={len(failed)}"
        for name, actual, expected in failed:
            text += f"\n[receipt] FAILED {name}: {actual!r} (expected {expected!r})"
        self.write("summary.txt", text + "\n")
        return text


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------
def sh(
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd or ROOT, env=env, capture_output=True, text=True, timeout=timeout)


def sh_out(args: list[str], env: dict[str, str] | None = None, timeout: int = 120) -> str:
    result = sh(args, env=env, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} exited {result.returncode}: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def wait_until(predicate: Callable[[], object], timeout: float = 120.0,
               interval: float = 1.0) -> bool:
    """Poll a condition until it is truthy or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - a probe that raises is not satisfied yet
            pass
        time.sleep(interval)
    return False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def filesystem_type(path: Path) -> str:
    """Filesystem of the mount covering `path`, from /proc/mounts (no shelling out)."""
    resolved = str(path.resolve())
    best_mount, best_type = "", "unknown"
    with open("/proc/mounts", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 3:
                continue
            mount, fstype = parts[1], parts[2]
            if (resolved == mount or resolved.startswith(mount.rstrip("/") + "/")) and len(mount) > len(best_mount):
                best_mount, best_type = mount, fstype
    return best_type


def git_state() -> dict:
    head = sh_out(["git", "rev-parse", "HEAD"])
    porcelain = sh_out(["git", "status", "--porcelain"]).splitlines()
    diff = subprocess.run(["git", "diff", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
    return {
        "head": head,
        "dirty_files": porcelain,
        "diff_head_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
    }


def cgroup_unified() -> bool:
    return Path("/sys/fs/cgroup/cgroup.controllers").exists()


def cgroup_controllers() -> list[str]:
    path = Path("/sys/fs/cgroup/cgroup.controllers")
    return path.read_text().split() if path.exists() else []


def command_version(argv: list[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    result = sh(argv, timeout=30)
    line = (result.stdout or result.stderr).strip().splitlines()
    return line[0] if result.returncode == 0 and line else None


def preflight(require_browser: bool = False, appdata_hint: Path | None = None,
              require_root: bool = False) -> tuple[dict, list[str]]:
    """Collect D1 environment facts; return (environment, missing prerequisites)."""
    missing: list[str] = []
    env: dict[str, object] = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "kernel": platform.release(),
        "cpu_model": platform.processor() or "unknown",
        "cpu_cores": os.cpu_count(),
        "cgroup_v2": cgroup_unified(),
        "cgroup_controllers": cgroup_controllers(),
        "locks": {name: sha256(path) for name, path in LOCKFILES.items()},
        "git": git_state(),
    }
    meminfo = Path("/proc/meminfo").read_text().splitlines()
    env["mem_total_kb"] = int(meminfo[0].split()[1])
    model = Path("/proc/cpuinfo").read_text()
    for line in model.splitlines():
        if line.startswith("model name"):
            env["cpu_model"] = line.split(":", 1)[1].strip()
            break
    if appdata_hint is not None:
        env["appdata_filesystem"] = filesystem_type(appdata_hint)

    docker_cli = shutil.which("docker")
    if docker_cli is None:
        missing.append("docker (the `docker` CLI is not on PATH)")
        missing.append("docker compose v2 (`docker compose version` needs the docker CLI)")
        missing.append("a container runtime: install Docker Engine (rootless is acceptable)")
    else:
        compose = sh(["docker", "compose", "version", "--short"], timeout=60)
        if compose.returncode != 0:
            missing.append("docker compose v2 (`docker compose version` failed)")
        else:
            env["compose_version"] = compose.stdout.strip()
        info = sh(["docker", "info", "--format", "{{.ServerVersion}}|{{.CgroupDriver}}|{{.OSType}}"], timeout=60)
        if info.returncode != 0:
            missing.append("a reachable Docker daemon (start it, or set DOCKER_HOST)")
        else:
            server, driver, os_type = info.stdout.strip().split("|")
            env["docker_server_version"] = server
            env["docker_cgroup_driver"] = driver
            env["docker_os_type"] = os_type
    env["docker_version"] = command_version(["docker", "--version"])
    env["chromium_version"] = command_version(["chromium", "--version"]) or command_version(["chromium-browser", "--version"])
    if require_browser and env["chromium_version"] is None:
        missing.append("chromium (browser phases drive it over CDP)")
    if require_root and os.geteuid() != 0:
        missing.append(
            "root privileges (the harness must chown appdata to "
            "$JARVIS_UID:$JARVIS_GID and bind-mount the ENOSPC tmpfs)")
    if not env["cgroup_v2"]:
        missing.append("cgroup v2 (`/sys/fs/cgroup` must be cgroup2fs; boot with cgroup_no_v1=all or systemd.unified_cgroup_hierarchy=1)")
    for controller in ("memory", "cpu"):
        if controller not in env["cgroup_controllers"]:
            missing.append(f"cgroup v2 `{controller}` controller (delegated to the container manager)")
    for name, path in LOCKFILES.items():
        if not path.exists():
            missing.append(f"{name} lockfile at {path.relative_to(ROOT)}")
    return env, missing


# ---------------------------------------------------------------------------
# D3 private network
# ---------------------------------------------------------------------------
def create_network() -> str:
    """Create (or reuse) the private bridge and return its gateway address."""
    if sh(["docker", "network", "inspect", NETWORK]).returncode == 0:
        return network_gateway()
    sh_out([
        "docker", "network", "create", "--driver", "bridge",
        "--subnet", NETWORK_SUBNET, "--gateway", NETWORK_GATEWAY, NETWORK,
    ])
    return network_gateway()


def network_gateway() -> str:
    return sh_out([
        "docker", "network", "inspect", "--format",
        "{{range .IPAM.Config}}{{.Gateway}}{{end}}", NETWORK,
    ])


def remove_network() -> None:
    sh(["docker", "network", "rm", NETWORK])


# ---------------------------------------------------------------------------
# D2 Compose wrapper: production file + the qualification override, per project
# ---------------------------------------------------------------------------
@dataclass
class Compose:
    project: str
    appdata: Path
    env: dict[str, str]
    owned_roots: tuple[Path, ...] = ()
    temporary_root: Path | None = None

    @property
    def env_port(self) -> int:
        """The host port this project publishes the dashboard on."""
        return int(self.env["DASHBOARD_HOST_PORT"])

    def command(self, *args: str) -> list[str]:
        flags = [flag for path in COMPOSE_FILES for flag in ("-f", path)]
        return ["docker", "compose", "-p", self.project, *flags, *args]

    def run(self, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        return sh(self.command(*args), env=self.env, timeout=timeout)

    def must_run(self, *args: str, timeout: int = 300) -> str:
        return sh_out(self.command(*args), env=self.env, timeout=timeout)

    def up(self, timeout: int = 300) -> str:
        return self.must_run("up", "-d", "--no-build", timeout=timeout)

    def build(self, timeout: int = 3600) -> str:
        result = self.run("build", timeout=timeout)
        return (result.stdout or "") + (result.stderr or "")

    def start(self) -> None:
        self.must_run("start")

    def stop(self, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return self.run("stop", timeout=timeout)

    def down(self, volumes: bool = False) -> None:
        args = ["down", "--quiet"] + (["-v"] if volumes else [])
        if volumes:
            # A volume is only ever destroyed inside this harness's temp root.
            for root in self.owned_roots:
                if not _is_within(root, self.temporary_root):
                    raise RuntimeError(f"refusing `down -v`: appdata {root} is outside the temporary root")
        self.run(*args)

    def container_id(self) -> str | None:
        out = self.run("ps", "--quiet", CONTAINER).stdout.strip()
        return out.splitlines()[0] if out else None

    def inspect(self) -> dict:
        cid = self.container_id()
        if cid is None:
            return {}
        return json.loads(sh_out(["docker", "inspect", cid]))[0]

    def logs(self) -> str:
        cid = self.container_id()
        return "" if cid is None else sh(["docker", "logs", cid], timeout=120).stdout

    def exec(self, *argv: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        return self.run("exec", CONTAINER, *argv, timeout=timeout)


def _is_within(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def qualification_env(appdata: Path, port: int, base_url: str, poll_seconds: int,
                      uid: int | None = None, gid: int | None = None) -> dict[str, str]:
    """The per-case environment for the production Compose file (plan D2)."""
    env = dict(os.environ)
    env.update({
        "DASHBOARD_BIND_ADDRESS": "127.0.0.1",
        "DASHBOARD_HOST_PORT": str(port),
        "JARVIS_APPDATA_DIR": str(appdata),
        "JARVIS_UID": str(uid if uid is not None else os.getuid()),
        "JARVIS_GID": str(gid if gid is not None else os.getgid()),
        "RADIANCE_BASE_URL": base_url,
        "JARVIS_SOURCE_ID": SOURCE_ID,
        "POLL_INTERVAL_SECONDS": str(poll_seconds),
    })
    return env


# ---------------------------------------------------------------------------
# AC1/D2 inspection expectations
# ---------------------------------------------------------------------------
def compose_document(path: Path = ROOT / "compose.yaml") -> dict[str, str]:
    """Structural facts about the shipped Compose file, without a YAML dependency."""
    text = path.read_text(encoding="utf-8")
    body = text.split(f"  {CONTAINER}:", 1)[1]
    env_block = body.split("    environment:", 1)[1].split("    mem_limit:", 1)[0]
    return {
        "environment_keys": sorted(
            line.strip().split(":", 1)[0] for line in env_block.splitlines() if ":" in line
        ),
        "source_id_value": _scalar(env_block, "SOURCE_ID"),
        "user": _scalar(body.split("    environment:")[0], "user"),
        "build_args_present": "\n      args:" in body.split("    image:")[0],
        "mem_limit": _scalar(body, "mem_limit"),
        "cpus": _scalar(body, "cpus"),
        "pids_limit": _scalar(body, "pids_limit"),
        "stop_grace_period": _scalar(body, "stop_grace_period"),
    }


def _scalar(block: str, key: str) -> str | None:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].strip().strip('"')
    return None


def inspect_expectations(inspect: dict) -> list[tuple[str, object, object]]:
    """(field, actual, expected) rows for the production fields D2 must not change."""
    host = inspect.get("HostConfig", {})
    log_config = host.get("LogConfig") or {}
    health = inspect.get("Config", {}).get("Healthcheck") or {}
    return [
        ("HostConfig.Memory", host.get("Memory"), 536_870_912),
        ("HostConfig.NanoCpus", host.get("NanoCpus"), 500_000_000),
        ("HostConfig.PidsLimit", host.get("PidsLimit"), 128),
        ("HostConfig.ReadonlyRootfs", host.get("ReadonlyRootfs"), True),
        ("HostConfig.CapDrop", sorted(host.get("CapDrop") or []), ["ALL"]),
        ("HostConfig.SecurityOpt", sorted(host.get("SecurityOpt") or []), ["no-new-privileges:true"]),
        ("HostConfig.Tmpfs", host.get("Tmpfs"), {"/tmp": "size=32m,mode=1777"}),
        ("HostConfig.RestartPolicy.Name", host.get("RestartPolicy", {}).get("Name"), "unless-stopped"),
        ("HostConfig.StopGracePeriod", host.get("StopGracePeriod"), 15_000_000_000),
        ("HostConfig.LogConfig.Type", log_config.get("Type"), "json-file"),
        ("HostConfig.LogConfig.Config.max-size", (log_config.get("Config") or {}).get("max-size"), "5m"),
        ("HostConfig.LogConfig.Config.max-file", (log_config.get("Config") or {}).get("max-file"), "3"),
        ("Config.Healthcheck.Test", health.get("Test"), ["CMD", "container-healthcheck"]),
        ("Config.Healthcheck.Interval", health.get("Interval"), 15_000_000_000),
        ("Config.Healthcheck.Timeout", health.get("Timeout"), 5_000_000_000),
        ("Config.Healthcheck.StartPeriod", health.get("StartPeriod"), 30_000_000_000),
        ("Config.Healthcheck.Retries", health.get("Retries"), 3),
    ]


def cgroup_dir(pid: int) -> Path:
    """The container's own cgroup directory, resolved from /proc/<pid>/cgroup."""
    text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    line = next((l for l in text.splitlines() if l.startswith("0::")), None)
    if line is None:
        raise RuntimeError(f"no unified cgroup line for pid {pid}: {text.strip()[:120]}")
    return Path("/sys/fs/cgroup") + line.split(":", 1)[1].strip("/")


def read_cgroup(cgroup_dir: Path, name: str) -> str | None:
    path = cgroup_dir / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def parse_key_values(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


NON_RECLAIMABLE_PRIMARY = ("anon", "shmem", "kernel")
NON_RECLAIMABLE_FALLBACK = ("kernel_stack", "slab_unreclaimable", "sock")


def non_reclaimable_bytes(memory_stat: str | None) -> int | None:
    """D7 margin measure: anon + shmem + kernel, or the documented fallback set."""
    stat = parse_key_values(memory_stat)
    if not stat:
        return None
    if "kernel" in stat:
        return sum(stat.get(key, 0) for key in NON_RECLAIMABLE_PRIMARY)
    return sum(stat.get(key, 0) for key in NON_RECLAIMABLE_FALLBACK)


# ---------------------------------------------------------------------------
# D6 samplers (host-side, read-only)
# ---------------------------------------------------------------------------
def proc_status(pid: int) -> dict[str, int]:
    wanted = {"VmRSS": "vm_rss_kb", "VmHWM": "vm_hwm_kb", "VmData": "vm_data_kb", "Threads": "threads"}
    out: dict[str, int] = {}
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        if key in wanted:
            out[wanted[key]] = int(rest.split()[0])
    return out


def fd_count(pid: int) -> int:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return -1


def established_sockets(pid: int) -> int:
    """Established TCP sockets in the container's network namespace."""
    total = 0
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/{pid}/net/{name}").read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        total += sum(1 for line in lines if len(line.split()) > 3 and line.split()[3] == "01")
    return total


def file_sizes(directory: Path, db_name: str = "dashboard.sqlite3") -> dict[str, int]:
    out: dict[str, int] = {}
    for suffix in ("", "-wal", "-shm"):
        path = directory / f"{db_name}{suffix}"
        out[path.name] = path.stat().st_size if path.exists() else 0
    return out


class OomWatcher:
    """`docker events --filter event=oom` for the whole run (D6/D7)."""

    def __init__(self, container: str, receipts: Receipts) -> None:
        self.container = container
        self.receipts = receipts
        self.lines: list[str] = []
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["docker", "events", "--filter", f"container={self.container}",
             "--filter", "event=oom", "--filter", "event=kill", "--filter", "event=die"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            self.lines.append(f"{time.strftime('%H:%M:%S', time.gmtime())} {line.strip()}")

    def stop(self) -> list[str]:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.receipts.write("docker-events.txt", "\n".join(self.lines) + "\n")
        return self.lines


class Sampler(threading.Thread):
    """D6 5-second cgroup/proc sampler loop; appends to `samples.jsonl`."""

    def __init__(self, pid: int, cgroup_dir: Path, receipts: Receipts,
                 container_lookup: Callable[[], str | None],
                 appdata: Path | None = None, interval_s: float = 5.0) -> None:
        super().__init__(daemon=True)
        self.pid = pid
        self.cgroup_dir = cgroup_dir
        self.receipts = receipts
        self.container_lookup = container_lookup
        self.appdata = appdata
        self.interval_s = interval_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=30)

    def run(self) -> None:
        next_slow = 0.0
        while not self._stop.is_set():
            started = time.time()
            try:
                self.receipts.jsonl("samples.jsonl", self.sample(now=started))
            except OSError as exc:  # a vanished pid is a finding, not a crash of the loop
                self.receipts.jsonl("samples.jsonl", {"t": started, "error": str(exc)})
            next_slow = self._slow_work(started, next_slow)
            self._stop.wait(max(0.0, self.interval_s - (time.time() - started)))

    def sample(self, now: float) -> dict:
        record: dict[str, object] = {"t": round(now, 3)}
        for name in ("memory.current", "memory.peak", "pids.current", "memory.events", "memory.stat", "cpu.stat"):
            text = read_cgroup(self.cgroup_dir, name)
            if name in ("memory.stat", "cpu.stat", "memory.events"):
                record[name.replace(".", "_")] = parse_key_values(text)
            elif text is not None:
                record[name.replace(".", "_")] = int(text.split()[0]) if text.split() else None
            else:
                record[name.replace(".", "_")] = None
        record["non_reclaimable_bytes"] = non_reclaimable_bytes(read_cgroup(self.cgroup_dir, "memory.stat"))
        record.update(proc_status(self.pid))
        record["fds"] = fd_count(self.pid)
        record["tcp_established"] = established_sockets(self.pid)
        return record

    def _slow_work(self, now: float, next_slow: float) -> float:
        """30 s docker stats/inspect and 60 s database sizes, from the same thread."""
        if next_slow and now < next_slow:
            return next_slow
        next_slow = now + self.interval_s
        cid = None
        with contextlib.suppress(Exception):
            cid = self.container_lookup()
        if cid:
            with contextlib.suppress(Exception):
                stats = json.loads(sh_out(["docker", "stats", "--no-stream", "--format", "{{json .}}", cid]))
                self.receipts.jsonl("docker-stats.jsonl", {"t": round(now, 3), **stats})
            with contextlib.suppress(Exception):
                state = json.loads(sh_out(["docker", "inspect", cid]))[0]["State"]
                self.receipts.jsonl("inspect.jsonl", {
                    "t": round(now, 3),
                    "status": state.get("Status"),
                    "health": (state.get("Health") or {}).get("Status"),
                    "oom_killed": state.get("OOMKilled"),
                    "exit_code": state.get("ExitCode"),
                    "started_at": state.get("StartedAt"),
                })
        if self.appdata is not None:
            self.receipts.jsonl("db-sizes.jsonl", {"t": round(now, 3), **file_sizes(self.appdata)})
        return next_slow


# ---------------------------------------------------------------------------
# run lifecycle (D8/D3 teardown)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def qualification_root(prefix: str) -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix=f"jarvis-{prefix}-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def fresh_appdata(path: Path, uid: int, gid: int) -> Path:
    """An appdata directory owned by the container's runtime UID/GID (plan D4).

    Mode 0770 matches the documented 0750 ownership preparation closely enough
    for the group to write, and never the forbidden 0777.
    """
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    path.chmod(0o770)
    return path


class FixtureControl:
    """Upstream fixture the container can reach, driven by counters/manifest.

    The container reaches it on the private network's gateway address (plan D3);
    the same object drives the designed cases by mutating the underlying
    `FixtureState` (status codes, uptime evidence, metric absence).
    """

    def __init__(self, host: str, port: int = 0, model: str = "fixture-model") -> None:
        self.model = model
        self.server = FixtureServer(model=model, host=host, port=port)
        self.models: list[dict] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "FixtureControl":
        self.server.start()
        return self

    def stop(self) -> None:
        self.server.stop()

    @property
    def state(self):  # FixtureState: the designed-case knobs
        return self.server.state

    @property
    def base_url(self) -> str:
        return self.server.base_url

    @property
    def port(self) -> int:
        return int(self.server.base_url.rsplit(":", 1)[1])

    # -- publishing --------------------------------------------------------
    def labels(self, model: str | None = None) -> dict[str, str]:
        return {"model_name": model or self.model, "engine": "0"}

    def load_manifest(self, manifest: dict | None) -> None:
        self.models = list((manifest or {}).get("models", []))
        origin = (manifest or {}).get("uptime_origin")
        if origin:
            self.state.uptime_origin = origin

    def set_series(self, ci: int, go: int, ri: int | None = None,
                   ro: int | None = None, model: str | None = None) -> None:
        install_fixture(self.state, self.labels(model), ci, go, ri, ro)

    def set_counters(self, rows: list[dict]) -> None:
        """Publish a manifest's `final_counters` rows for every model."""
        for row in rows:
            install_fixture(self.state, {"model_name": row["model_name"],
                                         "engine": row.get("engine", "0")},
                            *[int(row["counters"][key]) for key in COUNTER_KEYS])

    def advance(self, rows: list[dict], per_poll: dict[str, int]) -> list[dict]:
        """Advance counters by a fixed per-poll traffic profile and republish.

        Used by the resource workload, which needs a continuously growing
        upstream (plan Step 7.3); returns the new rows so the caller can record
        the difference it expects the dashboard to report.
        """
        advanced = []
        for row in rows:
            counters = {key: int(row["counters"][key]) + per_poll[key] for key in COUNTER_KEYS}
            advanced.append({"model_name": row["model_name"],
                             "engine": row.get("engine", "0"), "counters": counters})
        self.set_counters(advanced)
        return advanced

    def clear_series(self, model: str | None = None) -> None:
        for metric in ("radiance:prefill_tokens_total", "radiance:decode_tokens_total",
                       "vllm:prompt_tokens_total", "vllm:generation_tokens_total"):
            self.state.clear_metric(metric, self.labels(model))


@contextlib.contextmanager
def qualification_network() -> Iterator[str]:
    gateway = create_network()
    try:
        yield gateway
    finally:
        remove_network()


# ---------------------------------------------------------------------------
# Documented operations commands (plan Step 5: the doc lines ARE the executed
# commands, AC8). The case suite runs these strings verbatim with `sh -c` from
# the Compose directory, and `doc_consistency_check` requires each of them to
# appear verbatim in docs/deployment.md, so the two cannot drift apart.
# ---------------------------------------------------------------------------
BACKUP_COMMAND = (
    "docker compose exec dashboard jarvis-dashboard backup \\\n"
    "  --output /data/backups/dashboard-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
)
RESTORE_COMMANDS = [
    "docker compose stop",
    'docker run --rm \\\n'
    '  -v "$JARVIS_APPDATA_DIR:/data" \\\n'
    '  --user "$JARVIS_UID:$JARVIS_GID" \\\n'
    '  -e DATABASE_PATH=/data/dashboard.sqlite3 \\\n'
    '  -e RADIANCE_BASE_URL="$RADIANCE_BASE_URL" \\\n'
    '  -e SOURCE_ID="$JARVIS_SOURCE_ID" \\\n'
    '  --entrypoint jarvis-dashboard jarvis-dashboard:0.1.0 \\\n'
    '  restore --input /data/backups/dashboard-20261025T120000Z.sqlite3',
    "docker compose up -d",
]
UPGRADE_COMMANDS = ["docker compose stop", "docker compose up -d"]


def documented_commands() -> list[tuple[str, str]]:
    """(label, command text) pairs the case suite executes and the doc must carry."""
    rows = [("backup", BACKUP_COMMAND)]
    rows += [(f"restore {index + 1}", line) for index, line in enumerate(RESTORE_COMMANDS)]
    rows += [(f"upgrade {index + 1}", line) for index, line in enumerate(UPGRADE_COMMANDS)]
    return rows


def _fenced_blocks(text: str) -> list[str]:
    blocks, current = [], None
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


def _joined_lines(block: str) -> list[str]:
    """Shell lines with backslash continuations joined, whitespace collapsed."""
    lines, pending = [], ""
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        whole = " ".join((pending + line).split())
        pending = ""
        # `#` comment lines inside a block document a step, not a command.
        if not whole.startswith("#"):
            lines.append(whole)
    return lines


def doc_consistency_check(path: Path = DEPLOYMENT_DOC) -> tuple[list[tuple[str, object]],
                                                               list[tuple[str, object]]]:
    """Compare docs/deployment.md against the executed commands and AC1 facts.

    Returns (passed rows, failed rows) as (label, detail) pairs; the caller turns
    them into `[ok ]` / `[FAIL]` checks.
    """
    text = path.read_text(encoding="utf-8")
    commands = {line for block in _fenced_blocks(text) for line in _joined_lines(block)}
    passed: list[tuple[str, object]] = []
    failed: list[tuple[str, object]] = []

    def want(label: str, actual: object, expected: object) -> None:
        ok = actual == expected
        detail = expected if ok else f"actual={actual!r} expected={expected!r}"
        (passed if ok else failed).append((label, detail))

    for label, command in documented_commands():
        # Compared with the same normalisation the doc lines get: continuation
        # backslashes dropped, whitespace collapsed.
        flat = (_joined_lines(command) or [""])[0]
        want(f"documented command in deployment.md: {label}", flat in commands, True)

    # Phrases are matched against whitespace-normalised text: markdown wraps.
    lower = " ".join(text.lower().split())
    for stale in ("backend-only delivery", "delivery 9", "/srv/appdata", "24 h soak"):
        want(f"stale text removed: {stale}", stale not in lower, True)
    required = {
        ".env file beside compose.yaml": ".env` file next to `compose.yaml",
        "runtime user section": "## runtime user",
        "uid/gid ownership preparation": "install -d -o",
        "unraid section": "## unraid",
        "primary pool, no mover": "no secondary storage and no mover",
        "no smb/nfs": "not smb, not nfs",
        "private bind address, no login": "no login",
        "compose v2 prerequisite": "compose v2 is an operator prerequisite",
        "image moved by id": "docker save",
        "image loaded on the nas": "docker load",
        "log rotation inspection": "logconfig",
        "ac8 live check section": "live check against a real jarvis",
        "four metric names": "radiance:prefill_tokens_total",
        "uptime_s checked": "uptime_s",
        "one known request": "one small known request",
        "prefill caching caveat": "prefix caching",
        "collector-only restart": "docker compose restart dashboard",
        "radiance restart excluded": "no radiance restart",
        "non-root check": "docker top dashboard",
        "reader budget correction": "read_capacity = 4",
        'restore user flag': '--user "$jarvis_uid:$jarvis_gid"',
        "source id env name": '-e source_id="$jarvis_source_id"',
    }
    for label, needle in required.items():
        want(f"deployment.md documents: {label}", needle in lower, True)

    for label, actual, expected in compose_document_rows():
        want(f"compose.yaml: {label}", actual, expected)
    for label, actual, expected in dockerfile_check():
        want(f"Dockerfile: {label}", actual, expected)
    return passed, failed


def compose_document_rows() -> list[tuple[str, object, object]]:
    """AC1 fields that live in compose.yaml, checked without a YAML dependency."""
    doc = compose_document()
    return [
        ("SOURCE_ID is the app's own setting name", "SOURCE_ID" in doc["environment_keys"], True),
        ("JARVIS_SOURCE_ID is not passed as an env name",
         "JARVIS_SOURCE_ID" not in doc["environment_keys"], True),
        ("SOURCE_ID default is the source identity", doc["source_id_value"],
         "${JARVIS_SOURCE_ID:-jarvis}"),
        ("runtime user comes from JARVIS_UID/GID", (doc["user"] or "").startswith("${JARVIS_UID:?"), True),
        ("no build args (the image is built once)", doc["build_args_present"], False),
        ("mem_limit", doc["mem_limit"], "512m"),
        ("cpus", doc["cpus"], "0.5"),
        ("pids_limit", doc["pids_limit"], "128"),
        ("stop_grace_period", doc["stop_grace_period"], "15s"),
    ]


def dockerfile_stages(text: str) -> list[tuple[str, list[str]]]:
    """(FROM target, instructions) for every stage, with line continuations joined."""
    stages: list[tuple[str, list[str]]] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        instruction = (pending + line).strip()
        pending = ""
        if instruction.startswith("FROM "):
            stages.append((instruction, []))
        elif stages:
            stages[-1][1].append(instruction)
    return stages


def dockerfile_check() -> list[tuple[str, object, object]]:
    """The image is built only from digest-pinned inputs and runs as a numeric user."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    from_lines = [line.strip() for line in text.splitlines() if line.startswith("FROM ")]
    rows: list[tuple[str, object, object]] = [
        ("every FROM is digest-pinned",
         all("@sha256:" in line for line in from_lines), True),
    ]
    for pin in PINNED_BASE_IMAGES:
        rows.append((f"base image pinned {pin.split('@')[0]}",
                     any(pin in line for line in from_lines), True))
    rows += [
        ("runtime USER is numeric", "USER ${APP_UID}:${APP_GID}" in text, True),
        ("image entrypoint is the CLI", 'ENTRYPOINT ["jarvis-dashboard"]' in text, True),
        ("default command is serve", 'CMD ["serve"]' in text, True),
        ("healthcheck uses the stdlib probe", "container-healthcheck" in text, True),
        ("no RUN in a stage FROM a shell-less base image",
         [base.split(" AS ")[0] for base, cmds in dockerfile_stages(text)
          if any(pin in base for pin in SHELL_LESS_BASES)
          and any(cmd.startswith("RUN") for cmd in cmds)],
         []),
    ]
    return rows


def fixture_reachability_check(port: int | None = None) -> tuple[bool, str]:
    """D3 gate: a container on `jarvis-qual` reaches the fixture on the gateway
    address, the fixture is not published on loopback, and loopback-only callers
    are unaffected. Requires Docker."""
    gateway = create_network()
    fixture = FixtureServer(model="qual-reach", host=gateway, port=port or 0).start()
    address = f"http://{gateway}:{fixture.base_url.rsplit(':', 1)[1]}"
    try:
        install_fixture(fixture.state, {"model_name": "qual-reach", "engine": "0"}, 10, 1)
        host_probe = urllib.request.urlopen(f"{address}/metrics", timeout=5).read()
        host_ok = b"radiance:prefill_tokens_total" in host_probe
        container = sh([
            "docker", "run", "--rm", "--network", NETWORK, "--entrypoint", "python", IMAGE, "-c",
            f"import urllib.request;print(urllib.request.urlopen('{address}/metrics',timeout=5).read().decode())",
        ], timeout=180)
        container_ok = "radiance:prefill_tokens_total" in container.stdout
        loopback_port = fixture.base_url.rsplit(":", 1)[1]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{loopback_port}/metrics", timeout=2)
            private = False
        except (OSError, urllib.error.URLError):
            private = True
        ok = host_ok and container_ok and private
        return ok, (
            f"host on gateway={host_ok}; container on {NETWORK}={container_ok}; "
            f"not published on loopback={private}"
        )
    except (OSError, urllib.error.URLError) as exc:
        return False, f"fixture reachability probe failed: {exc}"
    finally:
        fixture.stop()



@contextlib.contextmanager
def managed_compose(compose: Compose) -> Iterator[Compose]:
    """Always tears the containers down, including after a failure."""
    try:
        yield compose
    finally:
        with contextlib.suppress(Exception):
            compose.down()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local qualification preflight (plan D1)")
    parser.add_argument("--preflight", action="store_true", help="collect environment facts and check prerequisites")
    parser.add_argument("--out", type=Path, default=None, help="write environment.json into this directory")
    parser.add_argument("--browser", action="store_true", help="require chromium (browser phases)")
    parser.add_argument("--appdata", type=Path, default=None, help="record the filesystem of this appdata directory")
    parser.add_argument("--fixture-check", action="store_true",
                        help="D3 gate: prove the fixture is reachable from a container on the private network")
    args = parser.parse_args()
    if not args.preflight:
        parser.print_help()
        return 2
    env, missing = preflight(require_browser=args.browser, appdata_hint=args.appdata)
    for key, value in env.items():
        if key not in ("git", "locks"):
            receipt(key, value)
    receipt("git head", env["git"]["head"])
    receipt("dirty files", len(env["git"]["dirty_files"]))
    receipt("lock digests", {k: v[:12] for k, v in env["locks"].items()})
    if args.out is not None:
        Receipts(args.out).dump("environment.json", env)
        receipt("environment.json", str(args.out / "environment.json"))
    if args.fixture_check and not missing:
        ok, detail = fixture_reachability_check()
        receipt("fixture reachability", detail)
        if not ok:
            print("[FAIL] fixture reachability check", flush=True)
            return 1
    if missing:
        print("[FAIL] qualification prerequisites missing:", flush=True)
        for item in missing:
            print(f"  - {item}", flush=True)
        return 1
    print("[ok ] qualification prerequisites satisfied", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())


def day_label(moment: datetime) -> str:
    """Day bucket label in the app's format: `YYYY-MM-DD`."""
    return to_iso(moment)[:10]
