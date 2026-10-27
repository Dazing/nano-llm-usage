"""Standard-library Chrome DevTools Protocol driver for the frontend smoke.

Chromium is launched headless with `--remote-debugging-port=0`; the browser
WebSocket is driven with a hand-written RFC 6455 client (no websocket package).
Only the calls the Delivery 3 smoke needs are implemented, and every command
carries the page `sessionId` created through `Target.createTarget` +
`Target.attachToTarget {flatten: true}`.

Run `python scripts/browser_driver.py --self-test` to check the driver itself:
it launches Chromium, evaluates `1 + 1` on `about:blank`, writes a screenshot,
and prints `selftest ok`.
"""

from __future__ import annotations

import base64
import json
import queue
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LAUNCH_TIMEOUT = 10.0


class DriverError(RuntimeError):
    """The browser, the protocol, or a page evaluation did not behave."""


def encode(payload: bytes, opcode: int = 0x81) -> bytes:
    """A single masked frame (client → server): 0x81 text, 0x8A pong."""
    n = len(payload)
    head = bytearray([opcode])
    if n < 126:
        head.append(0x80 | n)
    elif n < 0x10000:
        head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    else:
        head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    mask = secrets.token_bytes(4)
    return bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class _Frames:
    """Byte-stream → (opcode, payload) frames, unmasked server frames."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = bytearray()

    def _read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise DriverError("the browser closed the DevTools socket")
            self._buffer += chunk
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def next(self) -> tuple[int, bytes]:
        first, second = self._read(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._read(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read(8), "big")
        if second & 0x80:  # masked (servers must not mask; handle it anyway)
            mask = self._read(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(length)))
        else:
            payload = self._read(length)
        return opcode, payload

    def send(self, payload: bytes, opcode: int = 0x81) -> None:
        self._sock.sendall(encode(payload, opcode))


class Browser:
    """A launched headless Chromium plus its browser-level DevTools socket."""

    def __init__(self, proc: subprocess.Popen, ws: _Frames, pid: int):
        self._proc = proc
        self._ws = ws
        self._pid = pid
        self._next_id = 0
        self._responses: dict[int, queue.Queue] = {}
        self._events: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._alive = True
        self._pages: list[Page] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # An idle DevTools transport goes deaf in headless Chromium (a page left alone for
        # ~25 s stops answering), so the browser pings the socket on its own schedule.
        self._keepalive = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive.start()

    def _keepalive_loop(self) -> None:
        while self._alive:
            time.sleep(5.0)
            if not self._alive:
                return
            try:
                with self._send_lock:
                    self._ws.send(b"", 0x9)  # WebSocket ping; Chromium answers with a pong
            except (DriverError, OSError):
                return

    # -- lifecycle ---------------------------------------------------------- #
    @classmethod
    def launch(cls, profile: Path, args: list[str]) -> "Browser":
        binary = shutil.which("chromium") or shutil.which("chromium-browser")
        if binary is None:
            raise DriverError("chromium not found on PATH")
        profile.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "--disable-background-networking",
                # Headless pages count as occluded: without these the renderer sleeps between
                # polls and CDP calls time out. Explicit freezing (setWebLifecycleState)
                # still works, so the app's own pause-on-hidden behaviour stays testable.
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-component-update",
                "--window-size=1440,900",
                *args,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        endpoint = _read_devtools_endpoint(proc)
        try:
            ws_url = _version_info(endpoint)
            ws = _handshake(ws_url)
        except Exception:
            proc.terminate()
            raise
        return cls(proc, ws, proc.pid)

    def close(self) -> None:
        self._alive = False
        for page in self._pages:
            page.clear_events()
        try:
            self.command("Browser.close")
        except DriverError:
            pass
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._ws._sock.close()

    @property
    def pid(self) -> int:
        return self._pid

    # -- protocol ----------------------------------------------------------- #
    def command(self, method: str, params: dict | None = None, session: str | None = None,
                timeout: float = 30.0) -> dict:
        with self._lock:
            self._next_id += 1
            message_id = self._next_id
            expected = session or "browser"
            self._responses[message_id] = queue.Queue()
        body: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session is not None:
            body["sessionId"] = session
        with self._send_lock:
            self._ws.send(json.dumps(body).encode())
        replies = self._responses[message_id]
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._responses.pop(message_id, None)
                raise DriverError(f"{method} timed out after {timeout:.0f}s")
            try:
                message = replies.get(timeout=remaining)
            except queue.Empty:
                self._responses.pop(message_id, None)
                raise DriverError(f"{method} timed out after {timeout:.0f}s")
            self._responses.pop(message_id, None)
            if "error" in message:
                raise DriverError(f"{method} failed: {message['error']}")
            return message.get("result", {})

    def events(self, method: str) -> list[dict]:
        return self._events.setdefault(method, [])

    def _read_loop(self) -> None:
        while True:
            try:
                opcode, payload = self._ws.next()
            except (DriverError, OSError):
                return
            if opcode == 8:
                return
            if opcode == 9:
                self._ws.send(payload, 0x8A)  # ping → pong
                continue
            if opcode != 1:
                continue
            try:
                message = json.loads(payload.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            method = message.get("method")
            if method is None:
                replies = self._responses.get(message.get("id"))
                if replies is not None:
                    replies.put(message)
                continue
            self._events.setdefault(method, []).append(message)

    # -- pages -------------------------------------------------------------- #
    def new_page(self, width: int = 1440, height: int = 900) -> "Page":
        target = self.command("Target.createTarget", {"url": "about:blank"})
        attached = self.command("Target.attachToTarget",
                                {"targetId": target["targetId"], "flatten": True})
        page = Page(self, attached["sessionId"], target["targetId"])
        self._pages.append(page)
        page.set_viewport(width, height)
        return page


def _read_devtools_endpoint(proc: subprocess.Popen) -> str:
    prefix = "DevTools listening on "
    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        line = proc.stderr.readline() if proc.stderr else ""
        if not line:
            if proc.poll() is not None:
                raise DriverError(f"chromium exited during launch (code {proc.returncode})")
            time.sleep(0.02)
            continue
        if prefix in line:
            return line.split(prefix, 1)[1].strip()
    proc.terminate()
    raise DriverError("chromium never printed a DevTools endpoint")


def _version_info(endpoint: str) -> str:
    # Chromium prints the page ws URL; /json/version on the same host:port
    # carries the browser target that accepts Target.* commands.
    host, _, rest = endpoint.split("//", 1)[1].partition("/")
    with urllib.request.urlopen(f"http://{host}/json/version", timeout=5) as response:
        info = json.loads(response.read())
    return info["webSocketDebuggerUrl"]


def _handshake(url: str) -> _Frames:
    host, _, rest = url.split("//", 1)[1].partition("/")
    path = "/" + rest.lstrip("/")
    host_name, _, port_text = host.partition(":")
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    sock = socket.create_connection((host_name, int(port_text or 80)), timeout=10)
    sock.sendall(request)
    reader = _BufferedSocket(sock)
    status, headers = reader.read_response()
    if status != 101:
        sock.close()
        raise DriverError(f"websocket handshake returned HTTP {status}")
    import hashlib

    expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if headers.get("sec-websocket-accept") != expect:
        sock.close()
        raise DriverError("websocket handshake Sec-WebSocket-Accept mismatch")
    return _Frames(sock)


class _BufferedSocket:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = bytearray()

    def _line(self) -> str:
        while b"\r\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise DriverError("websocket handshake closed early")
            self._buffer += chunk
        line, _, rest = bytes(self._buffer).partition(b"\r\n")
        self._buffer = bytearray(rest)
        return line.decode("latin-1")

    def read_response(self) -> tuple[int, dict[str, str]]:
        status_line = self._line()
        status = int(status_line.split(" ")[1])
        headers: dict[str, str] = {}
        while True:
            line = self._line()
            if not line:
                break
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return status, headers


class Page:
    """One browser tab: navigation, evaluation, input, and event capture."""

    def __init__(self, browser: Browser, session: str, target_id: str):
        self._browser = browser
        self.session = session
        self.target_id = target_id
        self.console: list[tuple[str, str, str]] = []  # (origin, level, text)
        self.requests: list[str] = []
        self._subscribed = {"Runtime.consoleAPICalled", "Log.entryAdded",
                           "Network.requestWillBeSent", "Network.responseReceived",
                           "Page.loadEventFired", "Page.javascriptDialogOpening",
                           "Runtime.exceptionThrown"}
        for method in ("Runtime.enable", "Log.enable", "Network.enable", "Page.enable"):
            self._command(method)

    def _command(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        return self._browser.command(method, params, session=self.session, timeout=timeout)

    # -- navigation --------------------------------------------------------- #
    def navigate(self, url: str, timeout: float = 30.0) -> None:
        self._command("Page.navigate", {"url": url})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                return
            time.sleep(0.05)
        raise DriverError(f"page never reached readyState=complete: {url}")

    def add_script_on_new_document(self, source: str) -> None:
        self._command("Page.addScriptToEvaluateOnNewDocument", {"source": source})

    def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        result = self._command(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        details = result.get("exceptionDetails")
        if details:
            text = json.dumps(details)[:400]
            raise DriverError(f"page evaluation threw: {text}")
        return result.get("result", {}).get("value")

    # -- emulation ---------------------------------------------------------- #
    def set_viewport(self, width: int, height: int) -> None:
        self._command("Emulation.setDeviceMetricsOverride",
                      {"width": width, "height": height, "deviceScaleFactor": 1,
                       "mobile": False})

    def set_visibility(self, hidden: bool) -> None:
        self._command("Page.setWebLifecycleState",
                      {"state": "frozen" if hidden else "active"})

    def set_media(self, feature: str, value: str) -> None:
        self._command("Emulation.setEmulatedMedia",
                      {"features": [{"name": feature, "value": value}]})

    def screenshot(self, path: Path) -> Path:
        data = self._command("Page.captureScreenshot", {"format": "png"})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data["data"]))
        return path

    # -- input -------------------------------------------------------------- #
    def click(self, selector: str) -> None:
        self.evaluate(f"document.querySelector({json.dumps(selector)}).click()")

    def tab(self, n: int = 1) -> None:
        for _ in range(n):
            for key_type in ("keyDown", "keyUp"):
                self._command("Input.dispatchKeyEvent", {
                    "type": key_type, "key": "Tab", "code": "Tab",
                    "windowsVirtualKeyCode": 9, "nativeVirtualKeyCode": 9,
                })

    # -- events ------------------------------------------------------------- #
    def _drain(self) -> None:
        events = self._browser.events
        for message in events("Runtime.consoleAPICalled"):
            if message.get("sessionId") not in (None, self.session):
                continue
            params = message.get("params", {})
            text = " ".join(str(arg.get("value", arg.get("description", "")))
                            for arg in params.get("args", []))
            self.console.append(("console", params.get("type", "log"), text))
        for message in events("Log.entryAdded"):
            if message.get("sessionId") not in (None, self.session):
                continue
            entry = message.get("params", {}).get("entry", {})
            self.console.append(("log", entry.get("level", "log"), entry.get("text", "")))
        for message in events("Runtime.exceptionThrown"):
            if message.get("sessionId") not in (None, self.session):
                continue
            details = message.get("params", {}).get("exceptionDetails", {})
            text = details.get("exception", {}).get("description") or details.get("text", "")
            self.console.append(("exception", "error", text))
        for message in events("Network.requestWillBeSent"):
            if message.get("sessionId") not in (None, self.session):
                continue
            self.requests.append(message.get("params", {}).get("request", {}).get("url", ""))

    def drain(self) -> None:
        """Fold buffered protocol events into `console` and `requests`."""
        self._drain()

    def clear_events(self) -> None:
        for method in self._subscribed:
            self._browser._events[method] = []
        self.console.clear()
        self.requests.clear()


def self_test() -> int:
    profile = Path(tempfile.mkdtemp(prefix="browser-driver-selftest-"))
    browser = Browser.launch(profile, [])
    try:
        page = browser.new_page(800, 600)
        answer = page.evaluate("1 + 1")
        if answer != 2:
            print(f"selftest failed: 1 + 1 evaluated to {answer!r}")
            return 1
        shot = page.screenshot(Path("/tmp/browser-driver-selftest.png"))
        print(f"screenshot {shot} ({shot.stat().st_size} bytes), pid={browser.pid}")
    finally:
        browser.close()
    print("selftest ok")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print("usage: browser_driver.py --self-test")
    sys.exit(2)
