"""Controllable local Prometheus/stats fixture (tests only).

Kept entirely outside the production package. All counters, labels, uptime,
delays, sizes and failures are under test control; binds loopback only so
verification never touches the LAN default endpoint.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

SELECTED_METRICS = (
    "radiance:prefill_tokens_total",
    "radiance:decode_tokens_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
)
# Optional cache-tracking counter: published only when a test installs it, so the
# default scrape stays byte-identical for suites that predate cache tracking.
CACHED_METRIC = "radiance:linear_cached_tokens_total"
# Live-panel draft counters: emitted only when a test sets them, so the default
# scrape stays byte-identical.
DRAFT_METRICS = (
    "radiance:draft_tokens_total",
    "radiance:draft_accepted_total",
)
# Radiance-shaped histogram families used by the representative-size mode.
HISTOGRAM_FAMILIES = (
    "radiance:prompt_eval_duration_seconds",
    "radiance:generation_duration_seconds",
    "radiance:time_to_first_token_seconds",
)
HISTOGRAM_BUCKETS = ("0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10")
SIZE_LAYERS = 48
SIZE_EXPERTS = 128



class FixtureState:
    def __init__(self, model: str = "fixture-model", clock: Callable[[], float] = time.monotonic):
        self.lock = threading.Lock()
        self.model = model
        # (metric, canonical label tuple string) -> raw value lexeme
        self.samples: dict[tuple[str, tuple[tuple[str, str], ...]], str] = {}
        self.uptime_s: float | None = 100.0
        # When set, /stats reports uptime_s as clock() - uptime_origin: a process
        # start time stays consistent across a seeded history and a live run.
        self.uptime_origin: float | None = None
        self.clock = clock
        self.stats_fail = False
        self.stats_body: str | None = None  # override whole /stats body
        self.metrics_status: int | None = None  # e.g. 500
        self.metrics_body: str | None = None  # override whole /metrics body
        self.delay_s = 0.0
        self.chunk_delay_s = 0.0
        self.chunk_bytes = 65536
        self.oversize = False
        self.redirect = False
        self.requests: dict[str, int] = {}
        # Live fields (live-panel plan Step 4). Every default is unset, which
        # keeps /stats and /metrics byte-identical to the pre-live fixture.
        self.running: int | None = None
        self.waiting: int | None = None
        self.prefill_tps: float | None = None
        self.decode_tps: float | None = None
        self.experts: tuple[int, int] | None = None
        # Set to keep the experts block out of `/stats` while a running advancer
        # keeps updating it: the panel must show "no MoE", not "no data yet".
        self.suppress_experts = False  # (routed, routed_res)
        self.representative_size = False
        self.size_receipt: dict[str, object] = {}
        self._advancer: threading.Timer | None = None
        self._advance: dict[str, float] = {}
        self._labels_key: tuple[tuple[str, str], ...] = ()
        self._rates: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def set_live(
        self,
        *,
        running: int | None = None,
        waiting: int | None = None,
        prefill_tps: float | None = None,
        decode_tps: float | None = None,
        routed: int | None = None,
        routed_res: int | None = None,
    ) -> None:
        """Set the live ``/stats`` fields; the experts pair needs both halves."""
        with self.lock:
            self.running = running
            self.waiting = waiting
            self.prefill_tps = prefill_tps
            self.decode_tps = decode_tps
            self.experts = None if routed is None or routed_res is None else (routed, routed_res)

    def set_draft(self, labels: dict[str, str], tokens: int, accepted: int) -> None:
        self.set_series(labels, {DRAFT_METRICS[0]: tokens, DRAFT_METRICS[1]: accepted})

    def start_live_advancer(
        self,
        *,
        labels: dict[str, str],
        interval: float = 1.0,
        tokens_per_second: float = 100.0,
        accept_ratio: float = 0.7,
        experts_per_second: float = 100.0,
        resident_ratio: float = 0.93,
        running: int = 3,
        waiting: int = 1,
        prefill_tps: float = 1240.4,
        decode_tps: float = 180.2,
    ) -> None:
        """Advance the live counters on a fixed per-second background tick.

        A smoke run needs monotonically rising counters: the 5 s windows fill
        after ~6 s and the 1 m windows after ~62 s, and each tick adds exactly
        `accept_ratio` / `resident_ratio` of its own increments, which is the
        ratio the panel must display.
        """
        self._labels_key = tuple(sorted(labels.items()))
        self._rates = (tokens_per_second, accept_ratio, experts_per_second, resident_ratio)
        self.set_live(
            running=running,
            waiting=waiting,
            prefill_tps=prefill_tps,
            decode_tps=decode_tps,
            routed=0,
            routed_res=0,
        )
        self.set_draft(labels, 0, 0)
        with self.lock:
            self._advance = {"tokens": 0.0, "accepted": 0.0, "routed": 0.0, "routed_res": 0.0}
        self._schedule_tick(interval)

    def _schedule_tick(self, interval: float) -> None:
        timer = threading.Timer(interval, self._tick, args=(interval,))
        timer.daemon = True
        self._advancer = timer
        timer.start()

    def _tick(self, interval: float) -> None:
        tokens_per_second, accept_ratio, experts_per_second, resident_ratio = self._rates
        with self.lock:
            if not self._advance:
                return  # stopped
            self._advance["tokens"] += tokens_per_second
            self._advance["accepted"] = self._advance["tokens"] * accept_ratio
            self._advance["routed"] += experts_per_second
            self._advance["routed_res"] = self._advance["routed"] * resident_ratio
            self.samples[(DRAFT_METRICS[0], self._labels_key)] = str(int(self._advance["tokens"]))
            self.samples[(DRAFT_METRICS[1], self._labels_key)] = str(int(self._advance["accepted"]))
            self.experts = (int(self._advance["routed"]), int(self._advance["routed_res"]))
        self._schedule_tick(interval)

    def stop_live_advancer(self) -> None:
        with self.lock:
            self._advance = {}
            timer = self._advancer
        if timer is not None:
            timer.cancel()

    def use_representative_size(self) -> dict[str, object]:
        """Pad both bodies to a Radiance-shaped size and record the receipt."""
        with self.lock:
            self.representative_size = True
            self.size_receipt = {
                "mode": f"{SIZE_LAYERS}x{SIZE_EXPERTS} experts + histogram families",
                "stats_bytes": len(stats_body(self).encode("utf-8")),
                "metrics_bytes": len(metrics_body(self).encode("utf-8")),
            }
            return dict(self.size_receipt)


    def set_series(self, labels: dict[str, str], values: dict[str, object]) -> None:
        with self.lock:
            for metric, value in values.items():
                self.samples[(metric, tuple(sorted(labels.items())))] = str(value)

    def clear_metric(self, metric: str, labels: dict[str, str]) -> None:
        with self.lock:
            self.samples.pop((metric, tuple(sorted(labels.items()))), None)

    def drop_series(self, labels: dict[str, str]) -> None:
        with self.lock:
            for key in [k for k in self.samples if k[1] == tuple(sorted(labels.items()))]:
                del self.samples[key]

    def set_raw(self, metric: str, labels: dict[str, str], lexeme: str) -> None:
        with self.lock:
            self.samples[(metric, tuple(sorted(labels.items())))] = lexeme


def _render_line(metric: str, labels: tuple[tuple[str, str], ...], lexeme: str) -> str:
    if labels:
        rendered = ",".join(
            f'{k}="{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34)).replace(chr(10), chr(92) + "n")}"'
            for k, v in labels
        )
        return f"{metric}{{{rendered}}} {lexeme}"
    return f"{metric} {lexeme}"


def stats_body(state: FixtureState) -> str:
    """Render `/stats`; caller holds `state.lock` (so the receipt is consistent).

    With every live field unset the document is exactly the pre-live
    `{"model", "uptime_s"}` object.
    """
    uptime = state.uptime_s
    if state.uptime_origin is not None:
        uptime = state.clock() - state.uptime_origin
    document: dict[str, object] = {"model": state.model, "uptime_s": uptime}
    if state.running is not None:
        document["running"] = state.running
    if state.waiting is not None:
        document["waiting"] = state.waiting
    if state.prefill_tps is not None:
        document["prefill_tps"] = state.prefill_tps
    if state.decode_tps is not None:
        document["decode_tps"] = state.decode_tps
    experts: dict[str, object] = {}
    if state.experts is not None and not state.suppress_experts:
        experts["routed"], experts["routed_res"] = state.experts
    if state.representative_size:
        # Radiance reports per-layer, per-expert routing units and residency.
        experts["layer_units"] = [
            [1000 + layer * 10 + expert for expert in range(SIZE_EXPERTS)]
            for layer in range(SIZE_LAYERS)
        ]
        experts["layer_resident"] = [
            [1 if expert % 8 else 0 for expert in range(SIZE_EXPERTS)]
            for layer in range(SIZE_LAYERS)
        ]
    if experts:
        document["experts"] = experts
    return json.dumps(document)


def metrics_body(state: FixtureState) -> str:
    """Render `/metrics`; caller holds `state.lock`."""
    lines: list[str] = []
    for metric in SELECTED_METRICS + (CACHED_METRIC,) + DRAFT_METRICS:
        rows = [(labels, lexeme) for (name, labels), lexeme in state.samples.items() if name == metric]
        if rows:
            lines.append(f"# TYPE {metric} counter")
            lines.extend(_render_line(metric, labels, lexeme) for labels, lexeme in rows)
    if state.representative_size:
        labels = (("engine", "0"), ("model_name", state.model))
        for family in HISTOGRAM_FAMILIES:
            lines.append(f"# TYPE {family} histogram")
            cumulative = 0
            for index, le in enumerate(HISTOGRAM_BUCKETS):
                cumulative += 100 - index * 8
                lines.append(_render_line(f"{family}_bucket", labels + (("le", le),), str(cumulative)))
            lines.append(_render_line(f"{family}_sum", labels, f"{cumulative * 0.25:.3f}"))
            lines.append(_render_line(f"{family}_count", labels, str(cumulative)))
    return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    state: FixtureState

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        state = self.state
        with state.lock:
            state.requests[self.path] = state.requests.get(self.path, 0) + 1
            delay = state.delay_s
            redirect = state.redirect
        if delay:
            time.sleep(delay)
        if redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        if self.path == "/metrics":
            self._metrics(state)
        elif self.path == "/stats":
            self._stats(state)
        else:
            self.send_response(404)
            self.end_headers()

    def _metrics(self, state: FixtureState):
        with state.lock:
            if state.metrics_status is not None:
                self.send_response(state.metrics_status)
                self.end_headers()
                return
            body = state.metrics_body
            if body is None:
                body = metrics_body(state)
            oversize = state.oversize
            chunk_delay = state.chunk_delay_s
            chunk_bytes = state.chunk_bytes
        payload = body.encode("utf-8")
        if oversize:
            payload = payload + b"# pad " + b"x" * (2 * 1024 * 1024)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            for i in range(0, len(payload), chunk_bytes):
                self.wfile.write(payload[i : i + chunk_bytes])
                if chunk_delay:
                    time.sleep(chunk_delay)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stats(self, state: FixtureState):
        with state.lock:
            if state.stats_fail:
                self.send_response(500)
                self.end_headers()
                return
            if state.stats_body is not None:
                payload = state.stats_body.encode("utf-8")
            elif state.uptime_s is None and state.uptime_origin is None:
                self.send_response(500)
                self.end_headers()
                return
            else:
                payload = stats_body(state).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FixtureServer:
    def __init__(
        self,
        model: str = "fixture-model",
        host: str = "127.0.0.1",
        port: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Loopback by default; `host`/`port` let a harness publish it on a
        private bridge network so a container can reach it (qualification)."""
        self.state = FixtureState(model=model, clock=clock)
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FixtureServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FixtureServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def install_fixture(
    state: FixtureState,
    labels: dict[str, str],
    ci: object,
    go: object,
    ri: object | None = None,
    ro: object | None = None,
    ca: object | None = None,
) -> None:
    values: dict[str, object] = {
        SELECTED_METRICS[0]: ci,
        SELECTED_METRICS[1]: go,
    }
    if ri is not None:
        values[SELECTED_METRICS[2]] = ri
    if ro is not None:
        values[SELECTED_METRICS[3]] = ro
    if ca is not None:
        values[CACHED_METRIC] = ca
    state.set_series(labels, values)
