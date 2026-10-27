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
                lines = []
                for metric in SELECTED_METRICS:
                    rows = [(labels, lexeme) for (name, labels), lexeme in state.samples.items() if name == metric]
                    if rows:
                        lines.append(f"# TYPE {metric} counter")
                        lines.extend(_render_line(metric, labels, lexeme) for labels, lexeme in rows)
                body = "\n".join(lines) + "\n"
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
            else:
                uptime = state.uptime_s
                if state.uptime_origin is not None:
                    uptime = state.clock() - state.uptime_origin
                if uptime is None:
                    self.send_response(500)
                    self.end_headers()
                    return
                payload = json.dumps({"model": state.model, "uptime_s": uptime}).encode("utf-8")
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


def install_fixture(state: FixtureState, labels: dict[str, str], ci: object, go: object, ri: object | None = None, ro: object | None = None) -> None:
    values: dict[str, object] = {
        SELECTED_METRICS[0]: ci,
        SELECTED_METRICS[1]: go,
    }
    if ri is not None:
        values[SELECTED_METRICS[2]] = ri
    if ro is not None:
        values[SELECTED_METRICS[3]] = ro
    state.set_series(labels, values)
